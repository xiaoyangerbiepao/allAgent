# backend/core/retry.py
# 兜底机制：自动重试 → Agent 级降级 → 系统级兜底
#
# 注意职责边界（改造要点）：
#   本模块**不负责换模型**。换模型在 llm_factory 里做——get_llm 返回的本身就是一条
#   「主 → 备 1 → 备 2」的降级链，模型层的故障在那一层就消化掉了。
#   这里负责的是「整张图跑不通」之后怎么办：先重试（只对瞬态故障），
#   再退化成一段可读的提示，最后保证任何情况下都有响应返回。
#
#   为什么不能在这里换模型：这里包的是 graph.ainvoke，重跑一次会连带重跑
#   classify / HyDE / 向量检索 / 摘要，成本远高于换一个模型重发一次请求；
#   而且模型是在节点内部 get_llm() 取的，这一层根本不知道「该换成谁」。

import asyncio                                   # 异步：超时控制与等待
from functools import wraps                      # @wraps：保留原函数的元信息
from typing import Any, Callable, Optional       # 类型注解

import httpx                                     # 传输层异常分类
import openai                                    # 厂商 SDK 异常分类
from langchain_core.messages import AIMessage    # 降级结果要伪装成一条正常消息

from backend.core.exceptions import (            # 3.3 定义的异常
    LLMAPIError,
    MilvusConnectionError,
    InvalidInputError,
    AuthenticationError,
)
from backend.core.logger import get_logger, configure_logging

logger = get_logger(__name__)

# ── 不可重试：重试也不会变好，交给上层处理 ──────────────────────
NON_RETRYABLE_ERRORS = (
    InvalidInputError,        # 用户输入非法
    AuthenticationError,      # 我们自己的鉴权失败
)

MAX_RETRIES = 1                  # 只重试 1 次：模型层已有故障转移，再多只是拖长等待
RETRY_DELAYS = [2.0]             # 第 1 次重试前等 2 秒
TIMEOUT_PER_ATTEMPT = 150.0      # 整图执行上限（秒）。超时不重试，直接降级

# 明确「值得再试一次」的异常：厂商侧瞬态故障
_RETRYABLE_TYPES: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
    httpx.TransportError,
    ConnectionError,
    MilvusConnectionError,
    LLMAPIError,
)


def is_retryable(error: BaseException) -> bool:
    """判断这个异常重试一次有没有意义。

    之前这里写的是「except Exception 就重试」，后果是 401、400 也会老老实实重试
    3 轮，用户白等两分多钟才看到「Key 无效」。现在按类型判断。
    """
    if isinstance(error, NON_RETRYABLE_ERRORS):
        return False
    if isinstance(error, _RETRYABLE_TYPES):
        return True
    # 厂商返回的状态码里，408 / 429 / 5xx 属于瞬态；400 / 422 是我们请求本身的问题
    if isinstance(error, openai.APIStatusError):
        status = getattr(error, "status_code", 0)
        return status in (408, 409, 429) or status >= 500
    return False


def with_retry(agent_type: str = ""):
    """兜底装饰器工厂。给异步函数套上「重试 → 降级 → 系统兜底」三层保护。

    用法：
        @with_retry(agent_type="qa")
        async def _invoke():
            return await graph.ainvoke(state, config=config)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:

            # ── 第一层：自动重试 ──────────────────────────
            last_error: Optional[BaseException] = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    result = await asyncio.wait_for(
                        func(*args, **kwargs),
                        timeout=TIMEOUT_PER_ATTEMPT,
                    )
                    if attempt > 0:
                        logger.info("retry.succeeded", agent_type=agent_type, attempt=attempt + 1)
                    return result

                except NON_RETRYABLE_ERRORS as e:
                    logger.warning("retry.non_retryable_error", agent_type=agent_type, error=str(e))
                    raise

                except asyncio.TimeoutError as e:
                    # 超时不重试：再等 150 秒大概率还是超时，用户等不起
                    last_error = LLMAPIError(
                        f"执行超过 {TIMEOUT_PER_ATTEMPT:.0f} 秒未完成", agent_type=agent_type
                    )
                    logger.error(
                        "retry.timeout_no_retry", agent_type=agent_type,
                        timeout=TIMEOUT_PER_ATTEMPT, raw=str(e),
                    )
                    break

                except Exception as e:
                    last_error = e
                    if not is_retryable(e):
                        # 请求本身有问题（如 prompt 超长），换一次也不会对 → 直接降级
                        logger.warning(
                            "retry.skip_unretryable", agent_type=agent_type,
                            error_type=type(e).__name__, error=str(e)[:200],
                        )
                        break
                    if attempt < MAX_RETRIES:
                        delay = RETRY_DELAYS[attempt]
                        logger.warning(
                            "retry.attempt_failed", agent_type=agent_type,
                            attempt=attempt + 1, max_retries=MAX_RETRIES,
                            delay=delay, error=str(e)[:200],
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error("retry.all_attempts_failed", agent_type=agent_type, error=str(e)[:200])

            # ── 第二层：Agent 级降级 ──────────────────────
            try:
                fallback_result = await AgentFallbackHandler.handle(
                    agent_type=agent_type, original_error=last_error,
                )
                logger.info("retry.fallback_succeeded", agent_type=agent_type)
                return fallback_result
            except Exception as fallback_error:
                logger.error("retry.fallback_failed", agent_type=agent_type, error=str(fallback_error))

            # ── 第三层：系统级兜底 ────────────────────────
            logger.error("retry.system_fallback", agent_type=agent_type, original_error=str(last_error))
            return _system_fallback_response(agent_type)
        return wrapper
    return decorator


def _fallback_payload(content: str, **extra: Any) -> dict:
    """把降级文案包装成「和 graph 返回的 State 同构」的字典。

    这一步很关键：调用方（orchestrator）拿到的结果会被当成 LangGraph 的最终 State
    来读，它要的是 result_state["messages"][-1]。原先这里只返回 {"content": ...}，
    orchestrator 取 messages 时抛 KeyError，再被外层 except 兜成「系统处理请求时
    遇到问题」——精心写的降级文案一条都没送到用户手上。所以 messages 必须补齐。
    """
    payload: dict[str, Any] = {
        "messages": [AIMessage(content=content)],
        "content": content,
        "fallback_used": True,
        "structured_output": None,
    }
    payload.update(extra)
    return payload


class AgentFallbackHandler:
    """第二层降级：各 Agent 的专项降级策略（尽量保留核心功能，退化为更简单的实现）。"""

    @classmethod
    async def handle(cls, agent_type: str, original_error: BaseException) -> Any:
        """根据 agent_type 选择对应的降级策略。"""
        fallback_map = {                              # 类型 → 降级方法
            "qa":               cls._qa_fallback,
            "exam":             cls._exam_subjective_fallback,  # 编排层只传粗粒度 exam
            "exam_code":        cls._exam_code_fallback,
            "exam_subjective":  cls._exam_subjective_fallback,
            "resume":           cls._resume_fallback,
            "interview":        cls._interview_fallback,
        }
        handler = fallback_map.get(agent_type)
        if handler:
            return await handler()
        raise original_error                          # 没有对应降级策略 → 交给系统兜底

    @classmethod
    async def _qa_fallback(cls) -> dict:
        """问答降级：知识库或模型不可用，返回提示语。"""
        logger.info("fallback.qa_service_unavailable")
        return _fallback_payload(
            "⚠️ 智能问答暂时不可用（模型与知识库都没能响应），请稍后重试，"
            "或直接联系教师提问。"
        )

    @classmethod
    async def _exam_code_fallback(cls) -> dict:
        """代码批改降级：评分服务不可用，标记需教师人工复核。"""
        logger.info("fallback.exam_code_basic")
        return _fallback_payload(
            "代码评分服务暂时不可用，本次提交已标记为「需教师人工复核」。",
            needs_teacher_review=True,
            fallback_note="代码评分服务暂时不可用，已标记为需教师人工复核。",
        )

    @classmethod
    async def _exam_subjective_fallback(cls) -> dict:
        """简答题批改降级：标记需教师复核。"""
        logger.info("fallback.exam_subjective_basic")
        return _fallback_payload(
            "AI 评分服务暂时不可用，本次提交已标记为「需教师人工批改」。",
            needs_teacher_review=True,
            fallback_note="AI 评分服务暂时不可用，已标记为需教师人工批改。",
        )

    @classmethod
    async def _resume_fallback(cls) -> dict:
        """简历审查降级：提示服务不可用 / 检查文件。"""
        logger.info("fallback.resume_service_unavailable")
        return _fallback_payload(
            "简历审查服务暂时不可用，请稍后重试。如持续失败，请检查上传的 PDF 文件是否完整。"
        )

    @classmethod
    async def _interview_fallback(cls) -> dict:
        """面试降级：跳过深度分析，返回基础反馈。"""
        logger.info("fallback.interview_basic_feedback")
        return _fallback_payload(
            "面试评估服务暂时不可用，本次面试对话已记录，请稍后查看报告。"
        )


def _system_fallback_response(agent_type: str) -> dict:
    """第三层：系统级兜底。所有降级都失败后返回它，保证用户始终能收到响应。"""
    # agent_type 传进来的可能是 exam_subjective / exam_code，用前缀匹配才不会漏
    if agent_type.startswith("exam"):
        content = "非常抱歉，试卷批改服务暂时不可用，您的提交已保存，待服务恢复后将自动处理。"
    elif agent_type == "qa":
        content = "非常抱歉，智能问答服务暂时不可用，请稍后再试，或直接联系教师提问。"
    elif agent_type == "resume":
        content = "非常抱歉，简历审查服务暂时不可用，请稍后重新上传。"
    elif agent_type == "interview":
        content = "非常抱歉，模拟面试服务暂时不可用，请稍后重新开始。"
    else:
        content = "服务暂时不可用，请稍后再试。"

    return _fallback_payload(content, system_fallback=True)


if __name__ == '__main__':
    RETRY_DELAYS = [0.0]          # 测试时不用真等 2 秒
    configure_logging()
    calls = {"n": 0}

    @with_retry(agent_type="qa")
    async def ok():
        return "success"

    @with_retry(agent_type="qa")
    async def fail_once_then_ok():
        calls["n"] += 1
        if calls["n"] < 2:
            raise LLMAPIError("网络抖动")
        return "recovered"

    @with_retry(agent_type="qa")
    async def non_retryable():
        raise InvalidInputError("输入非法")

    @with_retry(agent_type="qa")
    async def always_fail():
        raise LLMAPIError("一直失败")

    async def main():
        print("① 正常:", await ok())
        print("② 重试成功:", await fail_once_then_ok())
        try:
            await non_retryable()
        except InvalidInputError as e:
            print("③ 不可重试直接抛出:", e)
        result = await always_fail()
        print("④ 走降级:", result["content"][:40], "| messages:", type(result["messages"][0]).__name__)

    asyncio.run(main())
