"""LLM access: the OpenAI chat model, a scripted model for tests, and call helpers."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.errors import ServiceUnavailable, UpstreamError
from app.logs import get_logger

log = get_logger(__name__)


class LLMNotConfigured(ServiceUnavailable):
    code = "llm_not_configured"


class LLMError(UpstreamError):
    code = "llm_error"


Usage = dict[str, int]
Responder = Callable[[list[BaseMessage], list[dict[str, Any]], Any], AIMessage]


def build_chat_model(settings: Settings) -> BaseChatModel:
    if not settings.llm_configured:
        key_name = "GEMINI_API_KEY" if settings.llm_provider == "gemini" else "OPENAI_API_KEY"
        raise LLMNotConfigured(f"The language model is not configured. Set {key_name} and LLM_MODEL on the server.")
    if settings.llm_provider == "gemini":
        # The native client is used rather than Gemini's OpenAI-compatible endpoint: Gemini requires
        # the thought signature of each tool call to be sent back with the tool result, and the
        # OpenAI client drops it, which makes every multi-step tool conversation fail with a 400.
        from langchain_google_genai import ChatGoogleGenerativeAI

        assert settings.gemini_api_key is not None and settings.llm_model is not None
        return ChatGoogleGenerativeAI(
            model=settings.llm_model,
            api_key=settings.gemini_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    from langchain_openai import ChatOpenAI

    assert settings.openai_api_key is not None and settings.llm_model is not None
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


class ScriptedChatModel(BaseChatModel):
    """Deterministic chat model driven by a Python function. Used by tests and harness self-checks.

    The responder receives the conversation, the bound tool definitions and the tool choice,
    and returns the next assistant message (optionally with tool calls).
    """

    responder: Any
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any) -> Runnable[LanguageModelInput, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        message = self.responder(messages, kwargs.get("tools") or [], kwargs.get("tool_choice"))
        if not isinstance(message, AIMessage):
            raise TypeError("scripted responder must return an AIMessage")
        if message.usage_metadata is None:
            prompt_chars = sum(len(message_text(m)) for m in messages)
            message.usage_metadata = {
                "input_tokens": prompt_chars // 4,
                "output_tokens": max(1, len(message_text(message)) // 4),
                "total_tokens": prompt_chars // 4 + max(1, len(message_text(message)) // 4),
            }
        return ChatResult(generations=[ChatGeneration(message=message)])


def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def usage_of(message: BaseMessage) -> Usage:
    meta = getattr(message, "usage_metadata", None) or {}
    return {
        "llm_calls": 1,
        "input_tokens": int(meta.get("input_tokens", 0) or 0),
        "output_tokens": int(meta.get("output_tokens", 0) or 0),
    }


def add_usage(total: Usage, delta: Usage) -> Usage:
    for key in ("llm_calls", "input_tokens", "output_tokens"):
        total[key] = total.get(key, 0) + delta.get(key, 0)
    return total


class _Pacer:
    """Process-wide sliding-window limiter for model requests (all runs share one quota)."""

    def __init__(self) -> None:
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self, per_minute: int) -> None:
        if per_minute <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= 60:
                    self._sent.popleft()
                if len(self._sent) < per_minute:
                    self._sent.append(now)
                    return
                await asyncio.sleep(60 - (now - self._sent[0]) + 0.05)


_pacer = _Pacer()
_RETRY_DELAY_RE = re.compile(r"retry(?:Delay|[ _-]?after)?['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def rate_limit_delay(exc: BaseException) -> float | None:
    """Seconds to wait if ``exc`` is a provider rate-limit error, else ``None``."""
    text = str(exc)
    status = getattr(exc, "status_code", None)
    if status != 429 and "RateLimit" not in type(exc).__name__ and "RESOURCE_EXHAUSTED" not in text and " 429" not in text:
        return None
    match = _RETRY_DELAY_RE.search(text)
    return min(float(match.group(1)) + 1.0, 90.0) if match else 15.0


async def invoke(model: Runnable[LanguageModelInput, Any], messages: list[BaseMessage], usage: Usage) -> AIMessage:
    """Call the model: pace requests, wait out rate limits, and translate provider failures into one error type."""
    settings = get_settings()
    waited = 0.0
    while True:
        await _pacer.wait(settings.llm_requests_per_minute)
        try:
            result = await model.ainvoke(messages)
            break
        except (LLMNotConfigured, LLMError):
            raise
        except Exception as exc:
            delay = rate_limit_delay(exc)
            if delay is not None and waited + delay <= settings.llm_rate_limit_wait_seconds:
                log.warning("llm_rate_limited", wait_seconds=round(delay, 1), waited_so_far=round(waited, 1))
                waited += delay
                await asyncio.sleep(delay)
                continue
            if delay is not None:
                raise LLMError(
                    f"The language model's rate limit did not clear after waiting {waited:.0f} seconds. "
                    "The provider's quota may be exhausted; try again later or lower LLM_REQUESTS_PER_MINUTE.",
                    code="llm_rate_limited",
                ) from exc
            detail = re.sub(r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9_-]{20,}", "[REDACTED]", str(exc))[:300]
            raise LLMError(f"The language model request failed: {type(exc).__name__}: {detail}") from exc
    if not isinstance(result, AIMessage):
        raise LLMError("The language model returned an unexpected response")
    add_usage(usage, usage_of(result))
    return result


async def call_structured[T: BaseModel](model: BaseChatModel, schema: type[T], messages: list[BaseMessage], usage: Usage) -> T:
    """Get a validated object by forcing a single tool call whose arguments follow ``schema``."""
    bound = model.bind_tools([schema], tool_choice=schema.__name__)
    conversation = list(messages)
    last_error = "no structured output returned"
    for _ in range(2):
        reply = await invoke(bound, conversation, usage)
        if reply.tool_calls:
            try:
                return schema.model_validate(reply.tool_calls[0]["args"])
            except ValidationError as exc:
                last_error = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:6])
        conversation = [
            *messages,
            HumanMessage(f"Your previous answer was not valid ({last_error}). Call {schema.__name__} with valid arguments."),
        ]
    raise LLMError(f"The language model did not return valid structured output: {last_error}")
