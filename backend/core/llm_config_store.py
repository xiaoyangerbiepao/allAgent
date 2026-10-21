# backend/core/llm_config_store.py
#
# 大模型配置的「内存快照 + 数据库读写」层。
#
# 为什么需要这一层，而不是让 LLMFactory 直接查库：
#   1. LLMFactory.get_llm() 是同步函数，被 LangGraph 节点在同步 / 线程池上下文里调用，
#      函数内部不能 await 查库；
#   2. 每轮对话都可能取模型，查库会白白增加一次往返；
#   3. 配置变更频率极低（人工在设置页点保存），完全可以「启动加载一次 + 变更时刷新」。
#
# 因此数据流是：
#   启动 lifespan → reload() 把三张表读进内存
#   业务调用     → LLMFactory 只读内存（零 IO）
#   前端保存     → API 写库 → reload() 刷新快照 → LLMFactory.clear_cache()
#
# 注意：当前假设单进程部署（uvicorn 单 worker）。若将来开多 worker，
# 各进程内存快照会不一致，需要改成 Redis 发布订阅或在 reload 上加 TTL 轮询。

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# 业务 Agent 的中文名（前端左栏列表用，后端统一维护避免前后端各写一份）
# ──────────────────────────────────────────────────────────────
AGENT_LABELS: dict[str, str] = {
    "qa":              "智能问答",
    "exam_subjective": "简答题批改",
    "exam_code":       "代码题批改",
    "resume":          "简历审查",
    "interview":       "模拟面试",
    "intent":          "意图识别",
    "summarize":       "对话摘要",
}


@dataclass(frozen=True)
class ProviderConfig:
    """一个模型厂商（只含元数据，密钥在运行时按 api_key_env 现取）。"""
    id: str
    display_name: str
    base_url: str
    api_key_env: str                 # 如 "XIAOMI_API_KEY"
    thinking_payload: Optional[dict] # 关闭思考模式的请求体片段，None 表示不带
    is_enabled: bool = True

    @property
    def api_key(self) -> str:
        """按环境变量名取密钥；字段名是 settings 里的小写形式。"""
        return getattr(get_settings(), self.api_key_env.lower(), "") or ""

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class ModelInfo:
    """可选的单个模型。tier 用来区分省钱款（flash）和推理款（pro）。"""
    provider_id: str
    model_id: str
    display_name: str
    tier: str                        # "flash" | "pro"
    context_window: Optional[int]
    supports_tools: bool
    status: str                      # available / not_activated / error / unknown
    is_selectable: bool = True


@dataclass(frozen=True)
class LlmBinding:
    """某个业务 Agent 当前绑定的模型。"""
    agent_type: str
    provider_id: str
    model_id: str
    temperature: float = 0.0
    # 主模型不可用时是否允许自动切换到备选（见 llm_router.build_chain）
    failover_enabled: bool = True

    @property
    def label(self) -> str:
        return AGENT_LABELS.get(self.agent_type, self.agent_type)


# ──────────────────────────────────────────────────────────────
# 兜底默认值：数据库不可用（或表还没建）时保证服务仍能启动。
# 内容必须与 migrations.py 里的 seed 数据保持一致。
# ──────────────────────────────────────────────────────────────
_DEFAULT_PROVIDERS: list[ProviderConfig] = [
    ProviderConfig("deepseek", "DeepSeek", "https://api.deepseek.com/v1",
                   "DEEPSEEK_API_KEY", {"thinking": {"type": "disabled"}}),
    ProviderConfig("xiaomi", "小米 MiMo", "https://api.xiaomimimo.com/v1",
                   "XIAOMI_API_KEY", None),
    ProviderConfig("bailian", "阿里百炼",
                   "https://llm-n4uunzelwrrg9l26.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                   "BAILIAN_API_KEY", None),
]

_DEFAULT_MODELS: list[ModelInfo] = [
    ModelInfo("deepseek", "deepseek-flash",  "DeepSeek V4.1 Flash", "flash", 1048576, True, "available"),
    ModelInfo("deepseek", "deepseek-v4-pro", "DeepSeek V4 Pro",     "pro",   1048576, True, "available"),
    ModelInfo("xiaomi",   "mimo-v2.6-flash", "MiMo V2.6 Flash",     "flash", None,    True, "available"),
    ModelInfo("xiaomi",   "mimo-v2.6-pro",   "MiMo V2.6 Pro",       "pro",   None,    True, "available"),
    ModelInfo("bailian",  "qwen3.8-flash",   "通义千问 3.8 Flash",  "flash", None,    True, "available"),
    ModelInfo("bailian",  "qwen3.8-max",     "通义千问 3.8 Max",    "pro",   None,    True, "available"),
]

_DEFAULT_BINDINGS: dict[str, LlmBinding] = {
    "qa":              LlmBinding("qa",              "deepseek", "deepseek-v4-pro", 0.30),
    "exam_subjective": LlmBinding("exam_subjective", "deepseek", "deepseek-v4-pro", 0.00),
    "exam_code":       LlmBinding("exam_code",       "deepseek", "deepseek-v4-pro", 0.00),
    "resume":          LlmBinding("resume",          "deepseek", "deepseek-v4-pro", 0.00),
    "interview":       LlmBinding("interview",       "deepseek", "deepseek-v4-pro", 0.50),
    "intent":          LlmBinding("intent",          "deepseek", "deepseek-flash",  0.00),
    "summarize":       LlmBinding("summarize",       "deepseek", "deepseek-flash",  0.00),
}


class LlmConfigStore:
    """配置快照单例。所有同步读取都走这里，不产生 IO。"""

    def __init__(self) -> None:
        self.providers: dict[str, ProviderConfig] = {p.id: p for p in _DEFAULT_PROVIDERS}
        self.models: dict[tuple[str, str], ModelInfo] = {
            (m.provider_id, m.model_id): m for m in _DEFAULT_MODELS
        }
        self.bindings: dict[str, LlmBinding] = dict(_DEFAULT_BINDINGS)
        self.loaded: bool = False          # 是否成功从 DB 加载过（False = 用的兜底常量）
        self.loaded_at: float = 0.0

    # ── 读取（同步，零 IO）──────────────────────────────────────
    def get_binding(self, agent_type: str) -> LlmBinding:
        """取业务绑定；DB 里没有该业务时回落默认值，再没有就报错（早暴露）。"""
        binding = self.bindings.get(agent_type)
        if binding:
            return binding
        if agent_type in _DEFAULT_BINDINGS:
            return _DEFAULT_BINDINGS[agent_type]
        raise ValueError(
            f"未知 agent_type: '{agent_type}'，可用类型：{sorted(AGENT_LABELS)}"
        )

    def get_provider(self, provider_id: str) -> Optional[ProviderConfig]:
        return self.providers.get(provider_id)

    def get_model(self, provider_id: str, model_id: str) -> Optional[ModelInfo]:
        return self.models.get((provider_id, model_id))

    # ── 加载（异步，启动 / 变更时调用）──────────────────────────
    async def reload(self) -> None:
        """从数据库重读三张表。失败时保留现有快照并记录告警，不阻断启动。"""
        from backend.dependencies import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as session:
                p_rows = (await session.execute(text(
                    "SELECT id, display_name, base_url, api_key_env, "
                    "       thinking_payload, is_enabled "
                    "FROM llm_providers ORDER BY sort_order"
                ))).mappings().all()

                m_rows = (await session.execute(text(
                    "SELECT provider_id, model_id, display_name, tier, context_window, "
                    "       supports_tools, status, is_selectable "
                    "FROM llm_models ORDER BY provider_id, sort_order"
                ))).mappings().all()

                b_rows = (await session.execute(text(
                    "SELECT agent_type, provider_id, model_id, temperature, failover_enabled "
                    "FROM llm_bindings"
                ))).mappings().all()

            if p_rows:
                self.providers = {
                    r["id"]: ProviderConfig(
                        id=r["id"],
                        display_name=r["display_name"],
                        base_url=r["base_url"],
                        api_key_env=r["api_key_env"],
                        thinking_payload=r["thinking_payload"],
                        is_enabled=r["is_enabled"],
                    )
                    for r in p_rows
                }
            if m_rows:
                self.models = {
                    (r["provider_id"], r["model_id"]): ModelInfo(
                        provider_id=r["provider_id"],
                        model_id=r["model_id"],
                        display_name=r["display_name"],
                        tier=r["tier"],
                        context_window=r["context_window"],
                        supports_tools=r["supports_tools"],
                        status=r["status"],
                        is_selectable=r["is_selectable"],
                    )
                    for r in m_rows
                }
            if b_rows:
                self.bindings = {
                    r["agent_type"]: LlmBinding(
                        agent_type=r["agent_type"],
                        provider_id=r["provider_id"],
                        model_id=r["model_id"],
                        temperature=float(r["temperature"]),
                        # 列可能因迁移未跑而为 None，按「开启」处理（与默认行为一致）
                        failover_enabled=(
                            True if r["failover_enabled"] is None else bool(r["failover_enabled"])
                        ),
                    )
                    for r in b_rows
                }

            self.loaded = True
            import time
            self.loaded_at = time.time()
            logger.info(
                "llm_config.loaded",
                providers=len(self.providers),
                models=len(self.models),
                bindings=len(self.bindings),
            )
        except Exception as e:
            logger.warning("llm_config.load_failed | fallback=常量默认值 | error=%s", e)

    # ── 写入（异步）──────────────────────────────────────────────
    async def save_binding(
        self,
        agent_type: str,
        provider_id: str,
        model_id: str,
        temperature: float,
        updated_by: Optional[str] = None,
        failover_enabled: Optional[bool] = None,
    ) -> LlmBinding:
        """UPSERT 一条绑定，然后刷新内存快照。调用方负责清 LLMFactory 缓存。

        failover_enabled 传 None 表示「不改这一项」，用 COALESCE 在 SQL 里保留原值。
        """
        from backend.dependencies import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO llm_bindings "
                    "  (agent_type, provider_id, model_id, temperature, failover_enabled, "
                    "   updated_by, updated_at) "
                    "VALUES (:agent_type, :provider_id, :model_id, :temperature, "
                    "        COALESCE(:failover_enabled, TRUE), :updated_by, NOW()) "
                    "ON CONFLICT (agent_type) DO UPDATE SET "
                    "  provider_id      = EXCLUDED.provider_id, "
                    "  model_id         = EXCLUDED.model_id, "
                    "  temperature      = EXCLUDED.temperature, "
                    "  failover_enabled = COALESCE(:failover_enabled, llm_bindings.failover_enabled), "
                    "  updated_by       = EXCLUDED.updated_by, "
                    "  updated_at       = NOW()"
                ),
                {
                    "agent_type": agent_type,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "temperature": Decimal(str(round(float(temperature), 2))),
                    "failover_enabled": failover_enabled,
                    "updated_by": updated_by,
                },
            )
            await session.commit()

        await self.reload()
        binding = self.get_binding(agent_type)
        logger.info(
            "llm_config.binding_saved",
            agent_type=agent_type,
            provider_id=provider_id,
            model_id=model_id,
            temperature=binding.temperature,
        )
        return binding

    async def update_model_probe(
        self,
        provider_id: str,
        model_id: str,
        status: str,
        detail: str = "",
        supports_tools: Optional[bool] = None,
    ) -> None:
        """回写某个模型的探测结果（/llm/sync 用）。"""
        from backend.dependencies import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE llm_models SET status = :status, status_detail = :detail, "
                    "  last_checked = NOW() "
                    "WHERE provider_id = :provider_id AND model_id = :model_id"
                ),
                {
                    "status": status,
                    "detail": (detail or "")[:500],
                    "provider_id": provider_id,
                    "model_id": model_id,
                },
            )
            # 单独更新 supports_tools：为 None 时不动原值，
            # 避免「只测了 chat、没测 tools」把已有能力标记冲掉
            if supports_tools is not None:
                await session.execute(
                    text(
                        "UPDATE llm_models SET supports_tools = :supports_tools "
                        "WHERE provider_id = :provider_id AND model_id = :model_id"
                    ),
                    {
                        "supports_tools": supports_tools,
                        "provider_id": provider_id,
                        "model_id": model_id,
                    },
                )
            await session.commit()

    # ── 给 API 用的目录视图 ─────────────────────────────────────
    def build_catalog(self) -> dict[str, Any]:
        """组装前端需要的完整目录：厂商 → 模型 → 当前绑定。不含任何密钥。"""
        providers: list[dict[str, Any]] = []
        for p in sorted(self.providers.values(), key=lambda x: x.display_name):
            models = [
                {
                    "model_id": m.model_id,
                    "display_name": m.display_name,
                    "tier": m.tier,
                    "context_window": m.context_window,
                    "supports_tools": m.supports_tools,
                    "status": m.status,
                    "is_selectable": m.is_selectable,
                }
                for m in self.models.values()
                if m.provider_id == p.id
            ]
            models.sort(key=lambda x: (x["tier"] != "flash", x["model_id"]))
            providers.append(
                {
                    "id": p.id,
                    "display_name": p.display_name,
                    # 只暴露「有没有配 Key」，不暴露 Key 本身
                    "has_key": p.has_key,
                    "enabled": p.is_enabled and p.has_key,
                    "models": models,
                }
            )

        agents = [
            {
                "agent_type": agent_type,
                "label": label,
                "binding": (
                    {
                        "provider_id": binding.provider_id,
                        "model_id": binding.model_id,
                        "temperature": binding.temperature,
                        "failover_enabled": binding.failover_enabled,
                    }
                    if (binding := self.bindings.get(agent_type))
                    else None
                ),
            }
            for agent_type, label in AGENT_LABELS.items()
        ]

        return {
            "agents": agents,
            "providers": providers,
            "loaded_from_db": self.loaded,
        }


# 模块级单例：全进程共享一份内存快照
store = LlmConfigStore()
