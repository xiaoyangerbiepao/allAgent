# backend/core/llm_health.py
#
# 模型厂商健康状态（熔断器）。
#
# 为什么需要它：
#   单纯「失败就切下一个」在真实故障下体验很差。DeepSeek 挂掉时，如果每次请求都
#   先老老实实连一次、等到超时才切，用户每个问题都要白等几十秒——比现在直接报错
#   还难受。熔断器的价值是：连续失败几次后把这个厂商「拉黑」一段时间，期间直接
#   跳过，用户无感；冷却期结束再放一个请求去试探（半开），成功就恢复。
#
# 三种状态：
#   closed（正常）  → 正常使用
#   open（熔断）    → 冷却期内跳过该厂商
#   half-open（半开）→ 冷却期已过，放行一次请求探测，成功则恢复，失败则继续熔断
#
# 计时用 time.monotonic()：不受系统时间调整影响（服务器校时会跳变）。
# 展示给前端用 time.time()：方便人看「最后失败时间」。

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.logger import get_logger

logger = get_logger(__name__)

# 连续失败多少次后熔断
FAILURE_THRESHOLD = 3
# 熔断后冷却多久（秒）。太短会反复打到坏厂商，太长会让恢复变慢
COOLDOWN_SECONDS = 60.0


@dataclass
class _Breaker:
    """单个厂商的熔断状态。"""
    consecutive_failures: int = 0     # 连续失败次数（成功即清零）
    total_failures: int = 0           # 累计失败次数（只统计，不清零，便于观察）
    opened_at: float = 0.0            # 熔断开启时刻（monotonic）；0 表示未熔断
    last_error: str = ""              # 最后一次失败原因（截断到 200 字）
    last_failure_wall: float = 0.0    # 最后一次失败的真实时间（给前端显示）
    last_success_wall: float = 0.0    # 最后一次成功的时间


class LlmHealth:
    """全进程共享的熔断器表。

    线程安全：get_llm 可能在事件循环里被调用，也可能在线程池里（如 retrieve 的
    run_in_executor），所以用 Lock 而不是依赖 asyncio 单线程假设。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._breakers: dict[str, _Breaker] = {}
        self._generation = 0          # 状态变更代数：LLMFactory 靠它判断要不要清快照

    # ── 代数 ───────────────────────────────────────────────────
    @property
    def generation(self) -> int:
        """每次「熔断开 / 恢复」都会 +1。工厂据此作废已缓存的模型链。"""
        return self._generation

    # ── 读 ─────────────────────────────────────────────────────
    def is_available(self, provider_id: str) -> bool:
        """该厂商当前能否使用。

        冷却期内的 open 状态返回 False（调用方跳过它）；
        冷却期已过返回 True（半开：放一次请求去试探，成败由 record_* 决定）。
        """
        with self._lock:
            breaker = self._breakers.get(provider_id)
            if breaker is None or breaker.opened_at == 0.0:
                return True
            if time.monotonic() - breaker.opened_at >= COOLDOWN_SECONDS:
                return True
            return False

    def remaining_cooldown(self, provider_id: str) -> float:
        """还剩多少秒冷却；未熔断返回 0。"""
        with self._lock:
            breaker = self._breakers.get(provider_id)
            if breaker is None or breaker.opened_at == 0.0:
                return 0.0
            elapsed = time.monotonic() - breaker.opened_at
            return max(0.0, COOLDOWN_SECONDS - elapsed)

    def state_of(self, provider_id: str) -> str:
        """返回 closed / open / half_open，给接口和前端用。"""
        with self._lock:
            breaker = self._breakers.get(provider_id)
            if breaker is None or breaker.opened_at == 0.0:
                return "closed"
            if time.monotonic() - breaker.opened_at >= COOLDOWN_SECONDS:
                return "half_open"
            return "open"

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """全部厂商的健康快照（不含任何密钥）。"""
        with self._lock:
            provider_ids = set(self._breakers)
        result: dict[str, dict[str, Any]] = {}
        for provider_id in provider_ids:
            with self._lock:
                breaker = self._breakers[provider_id]
                result[provider_id] = {
                    "state": self.state_of(provider_id),
                    "consecutive_failures": breaker.consecutive_failures,
                    "total_failures": breaker.total_failures,
                    "cooldown_remaining": round(self.remaining_cooldown(provider_id), 1),
                    "last_error": breaker.last_error,
                    "last_failure_at": breaker.last_failure_wall or None,
                    "last_success_at": breaker.last_success_wall or None,
                }
        return result

    # ── 写 ─────────────────────────────────────────────────────
    def record_success(self, provider_id: str) -> None:
        """一次成功调用：清零连续失败，若之前在熔断则恢复。"""
        with self._lock:
            breaker = self._breakers.setdefault(provider_id, _Breaker())
            was_open = breaker.opened_at != 0.0
            breaker.consecutive_failures = 0
            breaker.opened_at = 0.0
            breaker.last_success_wall = time.time()
            if was_open:
                self._generation += 1
        if was_open:
            logger.info("llm_health.provider_recovered", provider=provider_id)

    def record_failure(self, provider_id: str, error: str = "") -> None:
        """一次失败调用：累计；达到阈值则熔断。"""
        opened = False
        with self._lock:
            breaker = self._breakers.setdefault(provider_id, _Breaker())
            breaker.consecutive_failures += 1
            breaker.total_failures += 1
            breaker.last_error = (error or "")[:200]
            breaker.last_failure_wall = time.time()
            if breaker.opened_at == 0.0 and breaker.consecutive_failures >= FAILURE_THRESHOLD:
                breaker.opened_at = time.monotonic()
                self._generation += 1
                opened = True
                failures = breaker.consecutive_failures
        if opened:
            logger.warning(
                "llm_health.provider_opened | provider=%s failures=%s cooldown=%ss error=%s",
                provider_id, failures, COOLDOWN_SECONDS, (error or "")[:200],
            )

    def reset(self, provider_id: Optional[str] = None) -> None:
        """手工恢复（管理接口用）。不传厂商则全部恢复。"""
        with self._lock:
            targets = [provider_id] if provider_id else list(self._breakers)
            for pid in targets:
                breaker = self._breakers.get(pid)
                if breaker:
                    breaker.opened_at = 0.0
                    breaker.consecutive_failures = 0
            self._generation += 1
        logger.info("llm_health.reset", provider=provider_id or "all")


# 模块级单例：与 llm_config_store.store 一样，全进程共享一份
health = LlmHealth()
