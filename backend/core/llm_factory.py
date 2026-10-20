# backend/core/llm_factory.py
# LLM Factory：统一封装大模型调用，按 Agent 类型路由到「前端配置的那家厂商 + 那个模型」。
# 规矩：所有 Agent 必须通过此模块获取模型，禁止直接调用 init_chat_model。
#
# 与改造前的区别：
#   以前 model 名、api_key、base_url 全部硬编码成 DeepSeek 一家；
#   现在「哪个业务用哪个模型」存在数据库 llm_bindings 表里，
#   由 llm_config_store 在启动时加载成内存快照，本模块只读快照、不查库。
#   想换模型：前端设置页点一下即可，无需改代码、无需重启。

from typing import Type, Any, Optional                # 类型注解
from pydantic import BaseModel                        # 结构化输出的 Schema 都是它的子类
import httpx                                          # HTTP 客户端库（用来自定义网络行为）
from langchain.chat_models import init_chat_model      # 创建聊天模型
from langchain_core.language_models import BaseChatModel  # 聊天模型的基类（类型注解用）
from langchain_core.runnables import Runnable          # 「可运行对象」基类，结构化模型属于它

from backend.config import get_settings               # 读配置（API Key 等）
from backend.core.logger import get_logger            # 结构化日志
from backend.core.llm_config_store import (           # 配置快照 + 数据结构
    LlmBinding,
    ModelInfo,
    ProviderConfig,
    store,
)

logger = get_logger(__name__)                         # 本模块的日志器

# ── 自定义 httpx 客户端：绕过系统代理 ───────────────────────────
# 背景：Windows 系统代理或 HTTPS_PROXY 环境变量会被 httpx 默认探测到，
#       导致请求经代理后 TLS 握手失败。这几家国内都可直连，无需代理。
# trust_env=False 表示：完全忽略系统代理和相关环境变量。
_HTTP_ASYNC_CLIENT = httpx.AsyncClient(               # 异步客户端（给 ainvoke/astream 用）
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),       # 总超时 120 秒，建立连接超时 15 秒
)
_HTTP_SYNC_CLIENT = httpx.Client(                     # 同步客户端（给 invoke 用）
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),
)


class LLMFactory:
    """大模型工厂（统一获取模型的唯一入口）。

    用法（签名与改造前完全一致，存量调用无需改动）：
        llm = LLMFactory.get_llm("qa")                                  # 用当前绑定的模型
        structured = LLMFactory.get_structured_llm("resume", 某Schema)  # 结构化输出
        response = await llm.ainvoke(messages)
    """

    _instances: dict[str, BaseChatModel] = {}   # 模型实例缓存（缓存键 → 模型），全类共享

    @classmethod
    def _get_settings(cls):
        """内部小工具：取配置对象。"""
        return get_settings()

    @classmethod
    def _resolve(cls, agent_type: str) -> tuple[LlmBinding, ProviderConfig]:
        """查出该业务当前绑定的模型与厂商，并做一致性校验。

        任何一环不合法都直接抛错，而不是悄悄回落到别的模型——
        「静默换模型」会让排查变成噩梦：以为在用 A，实际一直在用 B。
        """
        binding = store.get_binding(agent_type)          # 未知 agent_type 会在这里抛 ValueError

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
                f"厂商 '{provider.id}' 未配置密钥："
                f".env.local 里缺少 {provider.api_key_env}"
            )

        model = store.get_model(binding.provider_id, binding.model_id)
        if model is None:
            raise ValueError(
                f"业务 '{agent_type}' 绑定的模型 "
                f"'{binding.provider_id}/{binding.model_id}' 不在可选清单里，请重新选择"
            )
        return binding, provider

    @classmethod
    def _build_model_kwargs(cls, provider: ProviderConfig, model_id: str) -> dict[str, Any]:
        """组装 init_chat_model 需要的参数（三家都走 OpenAI 兼容接口）。"""
        kwargs: dict[str, Any] = {
            "model": model_id,                                  # 厂商真实的模型名
            "model_provider": "openai",                         # 统一走 langchain-openai
            "api_key": provider.api_key,                        # 按 api_key_env 从 settings 现取
            "base_url": provider.base_url,                      # 来自 llm_providers 表
            "max_retries": 0,                                   # 模型层不重试；重试统一由 retry.py 管
            "http_async_client": _HTTP_ASYNC_CLIENT,            # 绕过代理的异步客户端
            "http_client": _HTTP_SYNC_CLIENT,                   # 同步客户端
        }

        # 「关闭思考模式」是厂商私有参数，只有支持的厂商才带。
        # DeepSeek 用 {"thinking":{"type":"disabled"}}；小米/百炼不认这个字段，
        # 强行传会被当成非法参数，所以它们在表里是 NULL。
        if provider.thinking_payload:
            kwargs["extra_body"] = provider.thinking_payload

        return kwargs

    @classmethod
    def get_llm(
        cls,
        agent_type: str,                       # Agent 类型，必须是 AGENT_LABELS 里的键
        temperature: Optional[float] = None,   # None = 用绑定里配的温度
        streaming: bool = False,               # 是否流式输出（问答/面试对话用）
    ) -> BaseChatModel:
        """按 Agent 类型获取模型实例（带缓存）。

        缓存键包含「厂商 + 模型 + 温度 + 是否流式」。
        厂商必须进缓存键：不同厂商可能有同名模型（比如 DeepSeek 和百炼都有
        deepseek-v4-pro），少了这一段，切换厂商后会命中旧实例，等于没切。
        """
        binding, provider = cls._resolve(agent_type)

        # 调用方没指定温度时，才用前端配置的温度
        temp = binding.temperature if temperature is None else float(temperature)

        cache_key = f"{provider.id}:{binding.model_id}:{temp}:{streaming}"
        if cache_key not in cls._instances:                 # 缓存里没有才新建
            kwargs = cls._build_model_kwargs(provider, binding.model_id)
            kwargs["temperature"] = temp
            kwargs["streaming"] = streaming
            llm = init_chat_model(**kwargs)                 # 真正创建模型
            cls._instances[cache_key] = llm                 # 存进缓存
            logger.info(
                "llm_factory.model_initialized",
                agent_type=agent_type,
                provider=provider.id,
                model=binding.model_id,
                temperature=temp,
                streaming=streaming,
            )
        return cls._instances[cache_key]

    @classmethod
    def get_structured_llm(
        cls,
        agent_type: str,
        output_schema: Type[BaseModel],        # 期望的输出结构（一个 Pydantic 模型类）
        temperature: Optional[float] = None,
    ) -> Runnable:
        """获取「绑定了结构化输出 Schema」的模型。

        结构化输出走 function calling，要求模型支持 tools。
        这里提前校验，避免跑到一半才发现该模型不支持工具调用。
        """
        binding, _ = cls._resolve(agent_type)
        model: Optional[ModelInfo] = store.get_model(binding.provider_id, binding.model_id)
        if model and not model.supports_tools:
            raise ValueError(
                f"业务 '{agent_type}' 绑定的模型 "
                f"'{binding.provider_id}/{binding.model_id}' 不支持工具调用，"
                f"无法用于结构化输出，请到设置页换一个 pro 模型"
            )

        llm = cls.get_llm(agent_type, temperature=temperature)
        # 绑定 Pydantic 结构；method="function_calling" 是这类兼容接口必须的
        return llm.with_structured_output(output_schema, method="function_calling")

    @classmethod
    def clear_cache(cls) -> None:
        """清空模型实例缓存。

        必须在前端保存新配置后调用，否则旧模型实例还在缓存里，
        用户会以为「切了但没生效」。
        """
        cls._instances.clear()
        logger.info("llm_factory.cache_cleared")

    @classmethod
    def describe(cls) -> dict[str, str]:
        """返回「业务 → 厂商/模型」的当前快照，排查问题时一眼看清在用谁。"""
        result: dict[str, str] = {}
        for agent_type in store.bindings:
            try:
                binding, provider = cls._resolve(agent_type)
                result[agent_type] = f"{provider.id}/{binding.model_id}"
            except ValueError as e:
                result[agent_type] = f"<不可用: {e}>"
        return result


# ── 模块级便捷函数（Agent 代码里的推荐写法）────────────────────────
# 比写 LLMFactory.get_llm(...) 更简洁，直接 from llm_factory import get_llm 即可。

def get_llm(
    agent_type: str,
    temperature: Optional[float] = None,
    streaming: bool = False,
) -> BaseChatModel:
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
    # 只打印当前路由，不发请求、不消耗额度。
    # 需要真实调用时，取消下面注释：
    #   import asyncio
    #   from langchain_core.messages import HumanMessage
    #   async def main():
    #       llm = LLMFactory.get_llm("qa", streaming=False)
    #       resp = await llm.ainvoke([HumanMessage(content="用一句话介绍 Python")])
    #       print(resp.text)
    #   asyncio.run(main())
    import json
    print(json.dumps(LLMFactory.describe(), ensure_ascii=False, indent=2))
    print("loaded_from_db:", store.loaded)
