# backend/core/observability.py
"""LangSmith 链路追踪接入。

关键点：LangChain 不需要显式初始化 SDK —— 它在每次 invoke 时读环境变量，
决定要不要把这次 run 上报给 LangSmith。所以这里的做法是：
把 .env.local 里的配置（pydantic 字段）映射成 LangChain 认识的环境变量，
之后项目里所有 ChatModel / Agent / Retriever / 工具调用都会被自动采集，
业务代码零改动。

必须保证在任何一次 LLM 调用之前执行（backend/main.py 模块级已调用）。
非 FastAPI 场景（如单独跑某个脚本）可以自己调一次：

    from backend.config import get_settings
    from backend.core.observability import init_langsmith_tracing
    init_langsmith_tracing(get_settings())
"""

import os

from backend.core.logger import get_logger

# langsmith Client 是进程内单例，首次创建时就把 api_url / api_key 定死了。
# 所以环境变量只能在"第一次 trace 之前"设置 —— 这也是本函数必须在启动最早期调用的原因。
_initialized = False


def init_langsmith_tracing(settings) -> bool:
    """按配置打开/关闭 LangSmith 上报。返回最终是否处于开启状态。

    幂等：重复调用只生效一次，避免热重载时反复覆盖环境变量。
    """
    global _initialized
    logger = get_logger(__name__)

    if _initialized:
        return os.environ.get("LANGSMITH_TRACING", "").lower() == "true"
    _initialized = True

    if not settings.langsmith_tracing:
        logger.info("langsmith.disabled")
        return False

    if not settings.langsmith_api_key:
        # 开关打开但没给 key：宁可不上报，也不要让 LangChain 抛 401 打断业务请求
        logger.warning("langsmith.skipped | reason=no_api_key")
        return False

    # 新版变量名（langsmith >= 0.2 / langchain-core >= 0.3）
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint

    # 旧版变量名（LANGCHAIN_*）：兼容按老文档写的依赖或第三方库，两套并存不冲突
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint

    # 采样率：<1 时只上报一部分 run。
    # 合同审查这类会把原文当 input 传出去的场景，建议 0.1~0.3，既够排查问题又少传敏感数据。
    rate = settings.langsmith_sampling_rate
    if 0 < rate < 1:
        os.environ["LANGSMITH_TRACING_SAMPLING_RATE"] = str(rate)

    # 只打项目名和端点，绝不打 key
    logger.info(
        "langsmith.ready | project=%s endpoint=%s sampling=%s",
        settings.langsmith_project,
        settings.langsmith_endpoint,
        "all" if rate >= 1 else rate,
    )
    return True
