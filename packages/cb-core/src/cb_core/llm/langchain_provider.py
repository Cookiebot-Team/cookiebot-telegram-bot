"""Langchain-backed provider: multi-provider model routing behind one client.

The two hand-rolled providers (`anthropic_provider.py`, `openai_provider.py`)
each speak to exactly one vendor's SDK. This one instead resolves a fully
qualified `"provider:model"` string (`"anthropic:claude-opus-5"`,
`"openai:gpt-4o-mini"`) through `langchain.chat_models.init_chat_model`, so a
task's model is configuration rather than a choice of provider module. It sits
behind the router exactly like the other two: same protocol, same breaker
(keyed by `name`), same metering — nothing downstream of `LLMRouter.complete()`
can tell which provider served a call.

Credential resolution is left to each integration package (`langchain-anthropic`,
`langchain-openai`), which read the vendor's usual environment variables. That
is why `build_router` can register this provider unconditionally: there is no
key to gate on at boot, only a call-time `LLMError` if a model turns out to be
unconfigured.

Speech-to-text has no portable interface across langchain's chat-model
integrations, so `transcribe` raises here and the `transcribe` task stays on
the OpenAI provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import anthropic
import openai
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from cb_core.llm.catalog import spec_for
from cb_core.llm.types import (
    Completion,
    LLMError,
    LLMRateLimitedError,
    Message,
    StopReason,
    Transcript,
    Usage,
)
from cb_core.logging import get_logger
from cb_core.settings import Settings

log = get_logger("cb.llm.langchain")

# role -> langchain message class. Total over `Role` (`llm/types.py`): a
# system-role entry inside the `messages` sequence is folded the same way the
# `system=` argument is, rather than being an unhandled case.
_ROLE_TO_MESSAGE: dict[str, type[BaseMessage]] = {
    "system": SystemMessage,
    "user": HumanMessage,
    "assistant": AIMessage,
}

# Resolved-client cache key, per R1.2: a hot path (the `chat` task, one config)
# must not re-resolve a client on every call.
_ClientKey = tuple[str, int, float | None, float | None]


class LangchainProvider:
    """Wraps `init_chat_model` behind the `LLMProvider` protocol.

    `settings` is accepted for symmetry with the other providers'
    construction and for any future langchain-wide option; credentials
    themselves are not read from it — see the module docstring.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[_ClientKey, BaseChatModel] = {}

    @property
    def name(self) -> str:
        return "langchain"

    # ------------------------------------------------------------------ setup

    def _resolve(
        self, model: str, max_tokens: int, temperature: float | None, timeout: float | None
    ) -> BaseChatModel:
        key: _ClientKey = (model, max_tokens, temperature, timeout)
        client = self._clients.get(key)
        if client is not None:
            return client

        kwargs: dict[str, Any] = {"max_tokens": max_tokens, "timeout": timeout}
        if temperature is not None:
            provider_prefix, bare_model = _split_model(model)
            spec = spec_for(bare_model, provider=provider_prefix)
            # Mirrors `anthropic_provider.py`'s own gating (`build_request`,
            # ~lines 98-102): current Claude models 400 on `temperature`, so a
            # task config that carries one (DEFAULT_TASKS["chat"] does, for v1
            # parity) must not reach a model whose spec says it will reject
            # it. Forwarding it unconditionally is exactly the bug this
            # gating fixes — every chat reply 400'd because the shipped
            # default model, claude-opus-5, has `supports_sampling=False`.
            if spec.supports_sampling:
                kwargs["temperature"] = temperature
            else:
                log.debug("llm.sampling_dropped", model=model, reason="model rejects it")

        # `init_chat_model` returns `_ConfigurableModel` only when a caller
        # passes `configurable_fields`, which this provider never does — the
        # overload below resolves to a plain `BaseChatModel`.
        resolved = init_chat_model(model, **kwargs)
        self._clients[key] = resolved
        return resolved

    # --------------------------------------------------------------- requests

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        temperature: float | None = None,
        effort: str | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
    ) -> Completion:
        client = self._resolve(model, max_tokens, temperature, timeout)
        try:
            response = await client.ainvoke(_to_langchain_messages(messages, system=system))
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc), _retry_after(exc)) from exc
        except openai.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc)) from exc
        except (anthropic.APIConnectionError, openai.APIConnectionError) as exc:
            raise LLMError(f"langchain connection error: {exc}") from exc
        except (anthropic.APIStatusError, openai.APIStatusError) as exc:
            raise LLMError(f"langchain {model} error: {exc}") from exc

        return _to_completion(response, model)

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        temperature: float | None = None,
        effort: str | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        client = self._resolve(model, max_tokens, temperature, timeout)
        try:
            async for chunk in client.astream(_to_langchain_messages(messages, system=system)):
                if chunk.text:
                    yield chunk.text
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc), _retry_after(exc)) from exc
        except openai.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc)) from exc
        except (anthropic.APIStatusError, openai.APIStatusError) as exc:
            raise LLMError(f"langchain {model} error: {exc}") from exc

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        # Tokenisation does not depend on sampling parameters, so this reuses
        # the resolved-client cache under a fixed key rather than the caller's
        # `max_tokens` (which `count_tokens` does not even receive).
        client = self._resolve(model, 0, None, None)
        try:
            return client.get_num_tokens_from_messages(
                _to_langchain_messages(messages, system=system)
            )
        except (anthropic.APIStatusError, openai.APIStatusError) as exc:
            raise LLMError(f"langchain count_tokens {model} error: {exc}") from exc

    async def transcribe(
        self, audio: bytes, *, model: str, filename: str = "audio.ogg", language: str | None = None
    ) -> Transcript:
        raise LLMError("langchain provider does not offer speech-to-text; route to openai")

    async def close(self) -> None:
        return None


# ------------------------------------------------------------------- mapping


def _split_model(model: str) -> tuple[str | None, str]:
    """Split a fully qualified `"provider:model"` string into
    `(provider_prefix, bare_model)`. Without a `provider:` qualifier there is
    nothing to strip and nothing to filter on, so the prefix is `None`.

    Shared by `_resolve` (catalog lookup for sampling-parameter gating) and
    `_to_completion` (catalog lookup for cost), so the two never drift on how
    a model string is parsed.
    """
    head, sep, tail = model.partition(":")
    return (head, tail) if sep else (None, head)


def _to_langchain_messages(messages: Sequence[Message], *, system: str | None) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    if system:
        out.append(SystemMessage(content=system))
    out.extend(_ROLE_TO_MESSAGE[m.role](content=m.content) for m in messages)
    return out


def _to_completion(response: AIMessage, requested_model: str) -> Completion:
    usage_md: Mapping[str, Any] = response.usage_metadata or {}
    input_tokens = usage_md.get("input_tokens", 0) or 0
    output_tokens = usage_md.get("output_tokens", 0) or 0
    input_details = usage_md.get("input_token_details") or {}
    cache_read = input_details.get("cache_read", 0) or 0
    usage = Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read
    )

    provider_prefix, bare_model = _split_model(requested_model)
    spec = spec_for(bare_model, provider=provider_prefix)

    return Completion(
        text=str(response.text),
        model=requested_model,
        provider="langchain",
        usage=usage,
        stop_reason=_normalise_stop(response.response_metadata),
        cost_usd=spec.cost_usd(input_tokens, output_tokens),
        raw=response,
    )


def _normalise_stop(metadata: dict[str, Any]) -> StopReason:
    # Anthropic integrations populate `stop_reason`; OpenAI ones populate
    # `finish_reason` under a different vocabulary. Both are handled so this
    # provider behaves the same as the two hand-rolled ones regardless of
    # which vendor it resolved to.
    stop_reason = metadata.get("stop_reason")
    if stop_reason in {"end_turn", "max_tokens", "stop_sequence", "tool_use", "refusal"}:
        return stop_reason

    match metadata.get("finish_reason"):
        case "stop":
            return "end_turn"
        case "length":
            return "max_tokens"
        case "tool_calls" | "function_call":
            return "tool_use"
        case "content_filter":
            return "refusal"
        case _:
            return "other"


def _retry_after(exc: anthropic.RateLimitError) -> float | None:
    header = getattr(getattr(exc, "response", None), "headers", None)
    if not header:
        return None
    raw = header.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
