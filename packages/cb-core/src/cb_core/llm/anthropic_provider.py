"""Anthropic provider.

Parameter filtering is the load-bearing part. Current Claude models **reject**
`temperature` / `top_p` / `top_k` and the old `thinking.budget_tokens` form with
a 400, and reject `thinking: {"type": "disabled"}` when effort is `xhigh` or
`max`. A generic wrapper that forwards whatever the caller passed would break on
exactly the models we default to, so every optional parameter is gated on the
model's catalog entry before it goes on the wire.

Refusals are a normal 200 response with `stop_reason == "refusal"`, not an
exception — indexing `content[0]` without checking would crash on them. Server-side
refusal fallbacks are enabled by default on models that support them, so a
declined request is re-served by a fallback model inside the same call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic

from cb_core.llm.catalog import ModelSpec, spec_for
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

log = get_logger("cb.llm.anthropic")

# Beta flag for the `fallbacks: "default"` scalar form (category-routed fallback).
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Effort levels that cannot be combined with disabled thinking.
_EFFORT_REQUIRING_THINKING = frozenset({"xhigh", "max"})


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
        refusal_fallback: bool = True,
    ) -> None:
        kwargs: dict[str, Any] = {"max_retries": max_retries, "timeout": timeout}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # A bare client also resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN /
        # an `ant auth login` profile, so an unset api_key is not an error here.
        self._client = anthropic.AsyncAnthropic(**kwargs)
        self._refusal_fallback = refusal_fallback

    @property
    def name(self) -> str:
        return "anthropic"

    # ------------------------------------------------------------------ requests

    def build_request(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        max_tokens: int,
        system: str | None,
        temperature: float | None,
        effort: str | None,
        thinking: bool | None,
    ) -> tuple[dict[str, Any], ModelSpec]:
        spec = spec_for(model, provider="anthropic")
        params: dict[str, Any] = {
            "model": spec.model_id,
            "max_tokens": min(max_tokens, spec.max_output),
            "messages": [
                {"role": m.role, "content": m.content} for m in messages if m.role != "system"
            ],
        }
        # Any system-role entries in the list are folded into the system prompt;
        # mid-conversation system messages are a separate, model-gated feature we
        # do not need here.
        system_parts = [m.content for m in messages if m.role == "system"]
        if system:
            system_parts.insert(0, system)
        if system_parts:
            params["system"] = "\n\n".join(system_parts)

        if temperature is not None:
            if spec.supports_sampling:
                params["temperature"] = temperature
            else:
                log.debug("llm.sampling_dropped", model=spec.model_id, reason="model rejects it")

        want_thinking = spec.thinking_on_by_default if thinking is None else thinking
        effective_effort = effort

        if spec.thinking == "adaptive":
            if want_thinking:
                params["thinking"] = {"type": "adaptive"}
            else:
                # Disabling thinking above `high` effort is a 400; and on these
                # models disabled thinking can emit a tool call as plain text or
                # leak <thinking> tags. Prefer low effort with thinking on.
                if effective_effort in _EFFORT_REQUIRING_THINKING:
                    log.info(
                        "llm.effort_capped",
                        model=spec.model_id,
                        requested=effective_effort,
                        applied="high",
                        reason="thinking disabled",
                    )
                    effective_effort = "high"
                params["thinking"] = {"type": "disabled"}

        if effective_effort and spec.supports_effort:
            params["output_config"] = {"effort": effective_effort}

        return params, spec

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
        params, spec = self.build_request(
            messages,
            model=model,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
            effort=effort,
            thinking=thinking,
        )
        client: Any = (
            self._client if timeout is None else self._client.with_options(timeout=timeout)
        )

        try:
            if self._refusal_fallback and spec.supports_fallbacks:
                response = await client.beta.messages.create(
                    **params, betas=[_FALLBACK_BETA], fallbacks="default"
                )
            elif params["max_tokens"] > spec.stream_above_max_tokens:
                # Large non-streaming requests hit SDK HTTP timeouts.
                async with client.messages.stream(**params) as stream:
                    response = await stream.get_final_message()
            else:
                response = await client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            retry_after = _retry_after(exc)
            raise LLMRateLimitedError(str(exc), retry_after) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"anthropic connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"anthropic {exc.status_code}: {exc.message}") from exc

        return self._to_completion(response, spec)

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
        params, _spec = self.build_request(
            messages,
            model=model,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
            effort=effort,
            thinking=thinking,
        )
        client: Any = (
            self._client if timeout is None else self._client.with_options(timeout=timeout)
        )
        try:
            async with client.messages.stream(**params) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc), _retry_after(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"anthropic {exc.status_code}: {exc.message}") from exc

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        spec = spec_for(model, provider="anthropic")
        payload: dict[str, Any] = {
            "model": spec.model_id,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages if m.role != "system"
            ],
        }
        if system:
            payload["system"] = system
        try:
            result = await self._client.messages.count_tokens(**payload)
        except anthropic.APIStatusError as exc:
            raise LLMError(f"anthropic count_tokens {exc.status_code}: {exc.message}") from exc
        return int(result.input_tokens)

    async def transcribe(
        self, audio: bytes, *, model: str, filename: str = "audio.ogg", language: str | None = None
    ) -> Transcript:
        raise LLMError("anthropic provider does not offer speech-to-text; route to openai")

    async def close(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------- parsing

    @staticmethod
    def _to_completion(response: Any, spec: ModelSpec) -> Completion:
        # `Any`: Message or BetaMessage depending on the call path taken; read only via getattr.
        stop_reason: StopReason = _normalise_stop(getattr(response, "stop_reason", None))
        category: str | None = None
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage_obj = response.usage
        usage = Usage(
            input_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
        )
        # `response.model` names whichever model actually served the turn, which
        # differs from the requested one when a refusal fallback fired.
        served = getattr(response, "model", spec.model_id)
        served_spec = spec_for(served, provider="anthropic")
        return Completion(
            text=text,
            model=served,
            provider="anthropic",
            usage=usage,
            stop_reason=stop_reason,
            refusal_category=category,
            cost_usd=served_spec.cost_usd(usage.input_tokens, usage.output_tokens),
            raw=response,
        )


def _normalise_stop(value: str | None) -> StopReason:
    if value in {"end_turn", "max_tokens", "stop_sequence", "tool_use", "refusal"}:
        return value  # type: ignore[return-value]
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
