# backend/api/v1/llm.py
#
# 大模型配置接口：让前端能自由切换「每个业务用哪家的哪个模型」。
#
# 安全边界（很重要）：
#   - 任何响应都不返回 API Key，只返回 has_key 布尔值；
#   - Key 始终留在 .env.local，数据库只存「变量名」；
#   - 写操作要求 teacher / admin 角色，学生只读不到这些接口（401/403）。
#
# 四个端点：
#   GET  /llm/catalog            厂商 + 可选模型 + 当前绑定（一次拿全，前端只调这一个）
#   PUT  /llm/bindings/{agent}   修改某个业务的绑定，立即生效（清缓存）
#   POST /llm/test               用指定厂商+模型发一条最小请求，验证是否真能调通
#   POST /llm/sync               逐个探测已上架模型，回写可用状态（约 20-40 秒）

import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.core import llm_health
from backend.core.llm_config_store import AGENT_LABELS, store
from backend.core.llm_factory import LLMFactory
from backend.core.llm_router import NO_CROSS_PROVIDER_AGENTS, describe_chain
from backend.core.logger import get_logger
from backend.dependencies import get_current_user

logger = get_logger(__name__)

router = APIRouter()

# 探测请求统一走这个客户端：trust_env=False 绕开系统代理（否则国内接口会被代理拦）
_HTTP = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(60.0, connect=15.0))


def _require_operator(user: dict) -> None:
    """写操作的角色校验：teacher 与 admin 可以改，student 一律拒绝。"""
    if user.get("role") not in ("teacher", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有教师或管理员可以修改模型配置",
        )


def _health_snapshot() -> dict[str, Any]:
    """所有厂商的健康状态。没出过故障的厂商也补成 closed，
    否则前端拿到空对象就没法区分「正常」和「字段没下发」。"""
    snapshot = llm_health.health.snapshot()
    for provider_id in store.providers:
        snapshot.setdefault(provider_id, {
            "state": "closed",
            "consecutive_failures": 0,
            "total_failures": 0,
            "cooldown_remaining": 0.0,
            "last_error": "",
            "last_failure_at": None,
            "last_success_at": None,
        })
    return snapshot


async def _probe_model(
    provider_id: str,
    model_id: str,
    with_tools: bool = False,
) -> dict[str, Any]:
    """对某个模型发一条最小请求，返回可用性结果。

    with_tools=True 时额外验证 function calling（结构化输出依赖它）。
    注意：这里不走 langchain，直接打 HTTP，避免污染 LLMFactory 的实例缓存。
    """
    provider = store.get_provider(provider_id)
    if provider is None:
        return {"ok": False, "latency_ms": 0, "detail": f"厂商 {provider_id} 不存在"}
    if not provider.has_key:
        return {"ok": False, "latency_ms": 0, "detail": f"缺少环境变量 {provider.api_key_env}"}

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": "只回复两个字：正常"}],
        "max_tokens": 32,
    }
    if with_tools:
        payload["tools"] = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询指定城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }]
        payload["tool_choice"] = "auto"
        payload["messages"] = [{"role": "user", "content": "北京今天天气如何"}]
        # 工具调用的输出比纯文本长得多，32 token 会被「思考模式」的推理内容吃光，
        # 导致明明支持 tools 的模型被误判成不支持（DeepSeek V4 就踩过这个坑）。
        payload["max_tokens"] = 256

    # 支持关思考的厂商，探测时也把思考关掉：否则推理 token 会抢占输出预算
    if provider.thinking_payload:
        payload.update(provider.thinking_payload)

    started = time.perf_counter()
    try:
        resp = await _HTTP.post(
            provider.base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        latency = int((time.perf_counter() - started) * 1000)

        if resp.status_code != 200:
            # 百炼的「产品未开通」会走到这里，错误文案要原样带出去，方便定位
            return {
                "ok": False,
                "latency_ms": latency,
                "detail": f"HTTP {resp.status_code}: {resp.text[:300]}",
                "not_activated": "not activated" in resp.text.lower(),
            }

        body = resp.json()
        message = body["choices"][0]["message"]
        if with_tools:
            return {
                "ok": True,
                "latency_ms": latency,
                "supports_tools": bool(message.get("tool_calls")),
                "detail": "支持工具调用" if message.get("tool_calls") else "返回 200 但未发起工具调用",
            }
        return {
            "ok": True,
            "latency_ms": latency,
            "content": (message.get("content") or "").strip()[:80],
            "detail": "调用成功",
        }
    except Exception as e:
        latency = int((time.perf_counter() - started) * 1000)
        return {"ok": False, "latency_ms": latency, "detail": f"{type(e).__name__}: {e}"}


# ──────────────────────────────────────────────────────────────
# GET /llm/catalog
# ──────────────────────────────────────────────────────────────
@router.get("/catalog", summary="厂商 / 可选模型 / 当前绑定 / 切换链 / 健康状态")
async def get_catalog(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """前端设置页只调这一个接口就能渲染完整界面。"""
    if not store.loaded:
        await store.reload()          # 启动时若 DB 未就绪，首次访问再补一次

    catalog = store.build_catalog()

    # 每个业务的「主 → 备」链：前端据此展示「DeepSeek 挂了会切到谁」。
    # 结构化输出的业务（批改/简历/面试）要求候选支持 tools，链会随之不同。
    for agent in catalog["agents"]:
        require_tools = agent["agent_type"] not in ("qa", "intent", "summarize")
        agent["chain"] = describe_chain(agent["agent_type"], require_tools=require_tools)
        # 批改类不做跨厂商切换：这是评分一致性的红线，前端要能看到原因
        agent["cross_provider_allowed"] = agent["agent_type"] not in NO_CROSS_PROVIDER_AGENTS

    catalog["health"] = _health_snapshot()
    catalog["agent_labels"] = AGENT_LABELS
    return catalog


# ──────────────────────────────────────────────────────────────
# PUT /llm/bindings/{agent_type}
# ──────────────────────────────────────────────────────────────
class BindingUpdate(BaseModel):
    provider_id: str = Field(..., description="厂商 id，如 xiaomi")
    model_id: str = Field(..., description="模型 id，如 mimo-v2.6-pro")
    temperature: Optional[float] = Field(None, ge=0, le=2, description="留空则沿用原温度")
    failover_enabled: Optional[bool] = Field(
        None, description="主模型不可用时是否自动切备选；留空则沿用原设置"
    )


@router.put("/bindings/{agent_type}", summary="修改某个业务的模型绑定")
async def update_binding(
    agent_type: str,
    payload: BindingUpdate,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _require_operator(user)

    if agent_type not in AGENT_LABELS:
        raise HTTPException(400, f"未知的业务类型：{agent_type}")

    provider = store.get_provider(payload.provider_id)
    if provider is None:
        raise HTTPException(400, f"厂商不存在：{payload.provider_id}")
    if not provider.has_key:
        raise HTTPException(
            400,
            f"厂商 {provider.id} 未配置密钥，请先在 .env.local 里设置 {provider.api_key_env}",
        )

    model = store.get_model(payload.provider_id, payload.model_id)
    if model is None:
        raise HTTPException(400, f"模型不存在：{payload.provider_id}/{payload.model_id}")
    if not model.is_selectable or model.status not in ("available", "unknown"):
        raise HTTPException(
            400,
            f"模型 {model.display_name} 当前不可选（状态：{model.status}），"
            f"请先点「同步校验」确认它能调通",
        )

    previous = store.get_binding(agent_type)
    temperature = previous.temperature if payload.temperature is None else payload.temperature

    binding = await store.save_binding(
        agent_type=agent_type,
        provider_id=payload.provider_id,
        model_id=payload.model_id,
        temperature=temperature,
        updated_by=user.get("user_id"),
        failover_enabled=payload.failover_enabled,
    )

    # 关键一步：不清缓存的话，旧模型实例还在内存里，用户会以为「切了没生效」
    LLMFactory.clear_cache()

    logger.info(
        "llm_api.binding_updated",
        agent_type=agent_type,
        provider=payload.provider_id,
        model=payload.model_id,
        failover=binding.failover_enabled,
        operator=user.get("user_id"),
    )
    return {
        "ok": True,
        "agent_type": agent_type,
        "binding": {
            "provider_id": binding.provider_id,
            "model_id": binding.model_id,
            "temperature": binding.temperature,
            "failover_enabled": binding.failover_enabled,
        },
        "chain": describe_chain(agent_type, require_tools=agent_type not in ("qa", "intent", "summarize")),
    }


# ──────────────────────────────────────────────────────────────
# POST /llm/test
# ──────────────────────────────────────────────────────────────
class ProbeRequest(BaseModel):
    provider_id: str
    model_id: str
    with_tools: bool = True


@router.post("/test", summary="测试某个厂商+模型是否可调用")
async def test_model(
    payload: ProbeRequest,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _require_operator(user)

    chat = await _probe_model(payload.provider_id, payload.model_id, with_tools=False)
    result: dict[str, Any] = {"chat": chat}
    if chat.get("ok") and payload.with_tools:
        result["tools"] = await _probe_model(payload.provider_id, payload.model_id, with_tools=True)
    result["ok"] = bool(chat.get("ok"))
    return result


# ──────────────────────────────────────────────────────────────
# POST /llm/sync
# ──────────────────────────────────────────────────────────────
@router.post("/sync", summary="逐个探测已上架模型并回写状态")
async def sync_models(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """只探测 llm_models 表里已上架的那几个模型（每家 flash/pro 各一个），
    不会把厂商 /models 返回的几百个目录灌进来。"""
    _require_operator(user)

    results: list[dict[str, Any]] = []
    for model in list(store.models.values()):
        if not model.is_selectable:
            continue

        chat = await _probe_model(model.provider_id, model.model_id, with_tools=False)
        supports_tools: Optional[bool] = None
        if not chat.get("ok"):
            new_status = "not_activated" if chat.get("not_activated") else "error"
        else:
            tools = await _probe_model(model.provider_id, model.model_id, with_tools=True)
            new_status = "available"
            supports_tools = bool(tools.get("supports_tools"))
            chat["supports_tools"] = supports_tools

        await store.update_model_probe(
            model.provider_id,
            model.model_id,
            status=new_status,
            detail=chat.get("detail", ""),
            supports_tools=supports_tools,
        )
        results.append({
            "provider_id": model.provider_id,
            "model_id": model.model_id,
            "status": new_status,
            "latency_ms": chat.get("latency_ms"),
            "supports_tools": chat.get("supports_tools"),
            "detail": chat.get("detail", "")[:200],
        })

    await store.reload()          # 把新状态刷进内存快照

    # 探测通过 = 该厂商当前确实活着，顺手把熔断解除，避免「明明好了还被跳过 60 秒」
    recovered = {r["provider_id"] for r in results if r["status"] == "available"}
    for provider_id in recovered:
        llm_health.health.record_success(provider_id)

    LLMFactory.clear_cache()      # 状态变了，实例也重建一次，避免用到坏配置

    available = sum(1 for r in results if r["status"] == "available")
    logger.info("llm_api.sync_done", total=len(results), available=available)
    return {"ok": True, "total": len(results), "available": available, "results": results}


# ──────────────────────────────────────────────────────────────
# GET /llm/health — 厂商熔断状态
# ──────────────────────────────────────────────────────────────
@router.get("/health", summary="各厂商熔断状态")
async def get_health(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """只读：谁在熔断、还剩几秒、最后一次失败原因。"""
    return {
        "health": _health_snapshot(),
        "generation": llm_health.health.generation,
        "threshold": llm_health.FAILURE_THRESHOLD,
        "cooldown_seconds": llm_health.COOLDOWN_SECONDS,
    }


# ──────────────────────────────────────────────────────────────
# POST /llm/health/reset — 手工解除熔断
# ──────────────────────────────────────────────────────────────
@router.post("/health/reset", summary="手工解除熔断（修好密钥/网络后用）")
async def reset_health(
    provider_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _require_operator(user)
    llm_health.health.reset(provider_id)
    LLMFactory.clear_cache()      # 链里可能还缺着刚恢复的厂商，重建一次
    return {"ok": True, "health": _health_snapshot()}
