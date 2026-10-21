# backend/core/llm_factory.py
# LLM Factory：统一封装大模型调用，按 Agent 类型路由到「前端配置的那家厂商 + 那个模型」。
# 规矩：所有 Agent 必须通过此模块获取模型，禁止直接 init_chat_model。
#
# 演进过程：
#   1.0  模型名、api_key、base_url 全部硬编码成 DeepSeek 一家；
#   2.0  改成「哪个业务用哪个模型」存数据库，前端可切；
#   3.0（当前）get_llm 返回的不再是单个模型，而是一条「主 → 备 1 → 备 2」的降级链。
#
# 为什么在工厂里做故障转移，而不是在 retry.py：
#   - retry.py 包的是整张 LangGraph 图，重跑一次要连带重跑 classify / HyDE / 检索；
#   - 模型是在节点内部 get_llm() 取的，retry 层看不见也指定不了「换谁」；
#   - 26 个调用点都是 llm.ainvoke(...)，改工厂只需一处，调用点零改动。
#
# 流式能力的边界（langchain_core 1.6.10 runnables/fallbacks.py 源码决定）：
#   RunnableWithFallbacks.astream 只会在「取到第一个 chunk 之前」失败时切换下一个；
#   一旦吐出过 token，后续异常直接抛出，不再切换。所以：
#     - 批改 / 简历 / 意图 / 摘要（非流式）：完全自动，用户无感；
#     - 流式问答：首 token 前自动切；首 token 后断流只能让前端重试。

import asyncio
from typing import Any, Optional, Type

import httpx
import openai
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from backend.config import get_settings
from backend.core import llm_health
from backend.core.llm_config_store import store
from backend.core.llm_router import ChainEntry, build_chain, describe_chain
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 自定义 httpx 客户端：绕过系统代理 ───────────────────────────
# 背景：Windows 系统代理或 HTTPS_PROXY 环境变量会被 httpx 默认探测到，
#       导致请求经代理后 TLS 握手失败。这几家国内都可直连，无需代理。
_HTTP_ASYNC_CLIENT = httpx.AsyncClient(
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),       # 总超时 120 秒，建连 15 秒
)
_HTTP_SYNC_CLIENT = httpx.Client(
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),
)

# ── 哪些异常值得「换一家试试」 ─────────────────────────────────
# 判断标准：故障出在「厂商侧」，换一家有可能成功。
#   ✔ 连不上 / 超时 / 5xx / 429 / Key 失效 / 模型下线 —— 都是这一家的问题
#   ✘ 400 / 422 —— 请求本身不合法（比如 prompt 超长），换谁都会报同样的错，
#     多打两次只是白白拉长等待
_FAILOVER_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,      # 连不上（DeepSeek 宕机大多是这个或 5xx）
    openai.APITimeoutError,         # 读超时
    openai.InternalServerError,     # 5xx
    openai.RateLimitError,          # 429
    openai.AuthenticationError,     # 401：Key 失效，这家废了但有别家可用
    openai.PermissionDeniedError,   # 403
    openai.NotFoundError,           # 404：模型下线
    httpx.TransportError,           # 传输层错误（含读超时、连接重置）
    asyncio.TimeoutError,
    ConnectionError,
)


class _ProviderHealthCallback(BaseCallbackHandler):
    """把「某次调用成功/失败」记到熔断器上。

    每个候选模型各自挂一个，回调里带 provider_id，所以能精确归因——不会出现
    「A 家失败导致 B 家被熔断」这种误伤。
    """

    def __init__(self, provider_id: str) -> None:
        super().__init__()
        self.provider_id = provider_id

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        llm_health.health.record_failure(self.provider_id, f"{type(error).__name__}: {error}")

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        llm_health.health.record_success(self.provider_id)


class LLMFactory:
    """大模型工厂（统一获取模型的唯一入口）。

    用法（签名与改造前完全一致，存量调用无需改动）：
        llm = LLMFactory.get_llm("qa")                                  # 主模型（不可用时自动切备选）
        structured = LLMFactory.get_structured_llm("resume", 某Schema)  # 结构化输出
        response = await llm.ainvoke(messages)
    """

    _instances: dict[str, Runnable] = {}   # 模型链缓存（缓存键 → 可运行对象），全类共享
    _health_generation: int = 0            # 上次同步时的熔断器代数

    # ── 内部工具 ───────────────────────────────────────────────
    @classmethod
    def _get_settings(cls):
        return get_settings()

    @classmethod
    def _sync_health_generation(cls) -> None:
        """熔断状态一变，已缓存的链就过期了（可能还指着坏掉的厂商）。

        用「代数」而不是逐条失效：一次 clear 就是 O(1)，实现简单且不会漏。
        """
        generation = llm_health.health.generation
        if generation != cls._health_generation:
            cls._instances.clear()
            cls._health_generation = generation
            logger.info("llm_factory.cache_invalidated_by_health", generation=generation)

    @classmethod
    def _build_model_kwargs(cls, entry: ChainEntry, temperature: float,
                            streaming: bool) -> dict[str, Any]:
        """组装 init_chat_model 需要的参数（三家都走 OpenAI 兼容接口）。"""
        kwargs: dict[str, Any] = {
            "model": entry.model_id,                            # 厂商真实的模型名
            "model_provider": "openai",                         # 统一走 langchain-openai
            "api_key": entry.provider.api_key,                  # 按 api_key_env 从 settings 现取
            "base_url": entry.provider.base_url,                # 来自 llm_providers 表
            "temperature": temperature,
            "streaming": streaming,
            "max_retries": 0,                                   # 模型层不重试；重试由 retry.py 管
            "http_async_client": _HTTP_ASYNC_CLIENT,            # 绕过代理的异步客户端
            "http_client": _HTTP_SYNC_CLIENT,
        }

        # 「关闭思考模式」是厂商私有参数，只有支持的厂商才带。
        # DeepSeek 用 {"thinking":{"type":"disabled"}}；小米/百炼不认这个字段，
        # 强行传会被当成非法参数，所以它们在表里是 NULL。
        if entry.provider.thinking_payload:
            kwargs["extra_body"] = entry.provider.thinking_payload

        return kwargs

    @classmethod
    def _build_runnable(
        cls,
        entry: ChainEntry,
        temperature: float,
        streaming: bool,
        output_schema: Optional[Type[BaseModel]] = None,
    ) -> Runnable:
        """造一个候选：模型实例（可选绑定结构化输出）+ 健康回调。"""
        llm: Any = init_chat_model(**cls._build_model_kwargs(entry, temperature, streaming))
        if output_schema is not None:
            # 结构化输出必须先绑在「具体模型」上，再参与 fallback：
            # with_structured_output 是 BaseChatModel 的方法，RunnableWithFallbacks 上没有
            llm = llm.with_structured_output(output_schema, method="function_calling")
        # 回调按候选挂在各自分支上，日志与熔断归因才准确
        return llm.with_config({"callbacks": [_ProviderHealthCallback(entry.provider.id)]})

    @classmethod
    def _assemble(
        cls,
        entries: list[ChainEntry],
        temperature: float,
        streaming: bool,
        output_schema: Optional[Type[BaseModel]] = None,
    ) -> Runnable:
        """把候选串成链：第一个是主，其余是备。只有一个时不做包装。"""
        runnables = [
            cls._build_runnable(e, temperature, streaming, output_schema) for e in entries
        ]
        if len(runnables) == 1:
            return runnables[0]
        return runnables[0].with_fallbacks(
            runnables[1:],
            exceptions_to_handle=_FAILOVER_ERRORS,
        )

    # ── 对外：普通模型 ─────────────────────────────────────────
    @classmethod
    def get_llm(
        cls,
        agent_type: str,                       # Agent 类型，必须是 AGENT_LABELS 里的键
        temperature: Optional[float] = None,   # None = 用绑定里配的温度
        streaming: bool = False,               # 是否流式输出（问答/面试对话用）
    ) -> Runnable:
        """按 Agent 类型获取「模型链」（带缓存）。

        缓存键包含整条链 + 温度 + 是否流式。链必须进缓存键：主模型被熔断后
        链首会变成备选，不换键就会一直复用那条指着坏厂商的旧链。
        """
        cls._sync_health_generation()

        entries = build_chain(agent_type)                 # 已按熔断状态过滤
        temp = entries[0].temperature if temperature is None else float(temperature)

        signature = "|".join(e.key for e in entries)
        cache_key = f"{signature}::{temp}::{streaming}"
        if cache_key not in cls._instances:
            runnable = cls._assemble(entries, temp, streaming)
            cls._instances[cache_key] = runnable
            logger.info(
                "llm_factory.model_initialized",
                agent_type=agent_type,
                chain=[e.key for e in entries],
                fallback_count=len(entries) - 1,
                temperature=temp,
                streaming=streaming,
            )
        return cls._instances[cache_key]

    # ── 对外：结构化输出 ───────────────────────────────────────
    @classmethod
    def get_structured_llm(
        cls,
        agent_type: str,
        output_schema: Type[BaseModel],        # 期望的输出结构（一个 Pydantic 模型类）
        temperature: Optional[float] = None,
    ) -> Runnable:
        """获取「绑定了结构化输出 Schema」的模型链。

        结构化输出走 function calling，要求模型支持 tools。这里提前校验主模型，
        并把链上不支持 tools 的候选直接剔除——否则跑到一半才发现，批改会静默出错。
        """
        cls._sync_health_generation()

        binding = store.get_binding(agent_type)
        primary_model = store.get_model(binding.provider_id, binding.model_id)
        if primary_model and not primary_model.supports_tools:
            raise ValueError(
                f"业务 '{agent_type}' 绑定的模型 "
                f"'{binding.provider_id}/{binding.model_id}' 不支持工具调用，"
                f"无法用于结构化输出，请到设置页换一个 pro 模型"
            )

        entries = build_chain(agent_type, require_tools=True)
        temp = entries[0].temperature if temperature is None else float(temperature)

        signature = "|".join(e.key for e in entries)
        cache_key = f"{signature}::{temp}::structured::{output_schema.__name__}"
        if cache_key not in cls._instances:
            runnable = cls._assemble(entries, temp, False, output_schema)
            cls._instances[cache_key] = runnable
            logger.info(
                "llm_factory.structured_model_initialized",
                agent_type=agent_type,
                chain=[e.key for e in entries],
                schema=output_schema.__name__,
            )
        return cls._instances[cache_key]

    # ── 运维辅助 ───────────────────────────────────────────────
    @classmethod
    def clear_cache(cls) -> None:
        """清空模型链缓存。

        必须在前端保存新配置后调用，否则旧链还在缓存里，
        用户会以为「切了但没生效」。
        """
        cls._instances.clear()
        logger.info("llm_factory.cache_cleared")

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """返回每个业务的切换链，排查问题时一眼看清「主是谁、会切到谁」。"""
        return {agent_type: describe_chain(agent_type) for agent_type in store.bindings}


# ── 模块级便捷函数（Agent 代码里的推荐写法）────────────────────────
# 比写 LLMFactory.get_llm(...) 更简洁，直接 from llm_factory import get_llm 即可。

def get_llm(
    agent_type: str,
    temperature: Optional[float] = None,
    streaming: bool = False,
) -> Runnable:
    """LLMFactory.get_llm 的便捷入口。"""
    return LLMFactory.get_llm(agent_type, temperature=temperature, streaming=streaming)


def get_structured_llm(
    agent_type: str,
    output_schema: Type[BaseModel],
    temperature: Optional[float] = None,
) -> Runnable:
    """LLMFactory.get_structured_llm 的便捷入口。"""
    return LLMFactory.get_structured_llm(agent_type, output_schema, temperature=temperature)


if __name__ == '__main__':
    # 只打印当前链路，不发请求、不消耗额度。
    import json
    print(json.dumps(LLMFactory.describe(), ensure_ascii=False, indent=2))
    print("loaded_from_db:", store.loaded)
    print("health:", json.dumps(llm_health.health.snapshot(), ensure_ascii=False, indent=2))
