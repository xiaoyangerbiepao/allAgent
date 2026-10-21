# backend/core/llm_router.py
#
# 模型切换链推导：给定一个业务（agent_type），算出「主模型挂了之后按什么顺序换」。
#
# 为什么单独一层，而不是写进 LLMFactory：
#   工厂负责「造实例」，路由负责「排顺序」。两者变化原因不同——换厂商、改档位策略
#   只需要动这里，工厂不需要重新理解业务语义。
#
# 三条策略（都不是拍脑袋，见每条的注释）：
#   1. 同档位优先：pro 挂了换 pro，不拿 flash 顶。批改/简历这类任务换成便宜模型，
#      质量掉得用户看得出来，但错误看不出来——这是最坏的一种「静默降级」。
#   2. 批改类禁止跨厂商：同一份卷子今天 82 分、明天 76 分，比「暂时不可用」更糟。
#      批改类宁可转人工复核，也不换一家模型来打分。
#   3. 链最长 3 个：主 + 2 备。再多只是把用户的等待时间拉长，不会提高成功率。

from dataclasses import dataclass
from typing import Optional

from backend.core import llm_health
from backend.core.llm_config_store import (
    LlmBinding,
    ModelInfo,
    ProviderConfig,
    store,
)

# ── 策略常量 ───────────────────────────────────────────────────
# 批改类 Agent：只允许同一厂商内换模型。跨厂商会改变评分口径。
NO_CROSS_PROVIDER_AGENTS: frozenset[str] = frozenset({"exam_subjective", "exam_code"})

# 是否允许「跨档位」兜底（flash 顶 pro）。默认关闭：
# 省钱款顶上去，用户看不出回答质量已经降了一档，属于静默降级。
ALLOW_CROSS_TIER: bool = False

# 一条链最多几个候选（含主模型）
MAX_CHAIN_LENGTH = 3

# 允许参与兜底的模型状态（unknown = 还没校验过，不能因此判死）
_ELIGIBLE_STATUS = ("available", "unknown")


@dataclass(frozen=True)
class ChainEntry:
    """链上的一个候选模型。"""
    provider: ProviderConfig
    model_id: str
    model: Optional[ModelInfo]
    temperature: float
    is_primary: bool = False

    @property
    def key(self) -> str:
        return f"{self.provider.id}/{self.model_id}"


def _provider_usable(provider: Optional[ProviderConfig]) -> bool:
    return bool(provider and provider.is_enabled and provider.has_key)


def _model_usable(model: Optional[ModelInfo], require_tools: bool) -> bool:
    if model is None:
        return False
    if not model.is_selectable or model.status not in _ELIGIBLE_STATUS:
        return False
    if require_tools and not model.supports_tools:
        return False
    return True


def _make_entry(
    provider: ProviderConfig,
    model_id: str,
    temperature: float,
    is_primary: bool = False,
) -> ChainEntry:
    return ChainEntry(
        provider=provider,
        model_id=model_id,
        model=store.get_model(provider.id, model_id),
        temperature=temperature,
        is_primary=is_primary,
    )


def _pick_same_provider_other_tier(binding: LlmBinding, provider: ProviderConfig,
                                   require_tools: bool) -> list[ChainEntry]:
    """同厂商换档位（pro ↔ flash）。只有 ALLOW_CROSS_TIER 打开时才参与。"""
    if not ALLOW_CROSS_TIER:
        return []
    entries: list[ChainEntry] = []
    for model in store.models.values():
        if model.provider_id != provider.id or model.model_id == binding.model_id:
            continue
        if _model_usable(model, require_tools):
            entries.append(_make_entry(provider, model.model_id, binding.temperature))
    return entries


def _pick_other_providers(binding: LlmBinding, require_tools: bool) -> list[ChainEntry]:
    """其他厂商的候选：先同档位，再（允许时）跨档位。

    顺序依赖 store.providers 的插入顺序，即 SQL 里的 sort_order（deepseek → xiaomi
    → bailian），保证每次算出来的链稳定可预测。
    """
    primary_model = store.get_model(binding.provider_id, binding.model_id)
    primary_tier = primary_model.tier if primary_model else "pro"

    same_tier: list[ChainEntry] = []
    other_tier: list[ChainEntry] = []
    for provider in store.providers.values():
        if provider.id == binding.provider_id or not _provider_usable(provider):
            continue
        for model in store.models.values():
            if model.provider_id != provider.id or not _model_usable(model, require_tools):
                continue
            entry = _make_entry(provider, model.model_id, binding.temperature)
            if model.tier == primary_tier:
                same_tier.append(entry)
            else:
                other_tier.append(entry)

    return same_tier + (other_tier if ALLOW_CROSS_TIER else [])


def build_chain(
    agent_type: str,
    require_tools: bool = False,
    respect_breaker: bool = True,
) -> list[ChainEntry]:
    """算出该业务的候选链，第一个是主模型。

    respect_breaker=True 时，处于熔断冷却期的厂商会被跳过——这是「不让用户白等」
    的关键。若所有候选都被熔断，则保留主模型（至少有真实错误可上报，而不是凭空
    编一个「全部不可用」）。
    """
    binding = store.get_binding(agent_type)          # 未知 agent_type 会抛 ValueError
    provider = store.get_provider(binding.provider_id)
    if provider is None:
        raise ValueError(
            f"业务 '{agent_type}' 绑定的厂商 '{binding.provider_id}' 不存在，"
            f"可用厂商：{sorted(store.providers)}"
        )
    if not provider.is_enabled:
        raise ValueError(f"厂商 '{provider.id}' 已被禁用，请到设置页更换模型")
    if not provider.has_key:
        raise ValueError(
            f"厂商 '{provider.id}' 未配置密钥：.env.local 里缺少 {provider.api_key_env}"
        )

    primary = _make_entry(provider, binding.model_id, binding.temperature, is_primary=True)
    chain: list[ChainEntry] = [primary]

    # 前端关掉自动切换 → 只有主模型，行为与改造前一致
    if binding.failover_enabled:
        chain.extend(_pick_same_provider_other_tier(binding, provider, require_tools))
        if agent_type not in NO_CROSS_PROVIDER_AGENTS:
            chain.extend(_pick_other_providers(binding, require_tools))

    # 去重（同厂商同档位那段可能和跨厂商段重叠），并截断到上限
    seen: set[str] = set()
    deduped: list[ChainEntry] = []
    for entry in chain:
        if entry.key in seen:
            continue
        seen.add(entry.key)
        deduped.append(entry)
    deduped = deduped[:MAX_CHAIN_LENGTH]

    if not respect_breaker or len(deduped) == 1:
        return deduped

    survivors = [e for e in deduped if llm_health.health.is_available(e.provider.id)]
    if not survivors:                    # 全挂了：保留主模型，让真实错误暴露出来
        return deduped[:1]
    # 主模型被熔断时，链首变成第一个可用候选；顺序保持相对不变
    return survivors


def describe_chain(agent_type: str, require_tools: bool = False) -> list[dict]:
    """给人看的链（设置页展示用）。不做熔断过滤，如实呈现「打算怎么切」。"""
    binding = store.get_binding(agent_type)
    try:
        entries = build_chain(agent_type, require_tools=require_tools, respect_breaker=False)
    except ValueError:
        return []

    result: list[dict] = []
    for entry in entries:
        healthy = llm_health.health.is_available(entry.provider.id)
        result.append({
            "provider_id": entry.provider.id,
            "provider_name": entry.provider.display_name,
            "model_id": entry.model_id,
            "display_name": entry.model.display_name if entry.model else entry.model_id,
            "tier": entry.model.tier if entry.model else "unknown",
            "is_primary": entry.is_primary,
            "healthy": healthy,
            "breaker": llm_health.health.state_of(entry.provider.id),
        })
    return result
