"""OpenAI provider — also covers every OpenAI-compatible endpoint.

Setting `base_url` points this at Ollama, vLLM, OpenRouter, LM Studio or any
other compatible server, so "self-host the model" is configuration rather than
code. This provider also carries speech-to-text, which the Anthropic one does
not offer — the router sends the `transcribe` task here.

v1 used this vendor for both chat and Whisper (`NaturalLanguage.py`, `Audio.py`)
with a module-level client, no retry policy, no timeout, and no token accounting.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai

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

log = get_logger("cb.llm.openai")


class OpenAIProvider:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
    ) -> None:
        kwargs: dict[str, Any] = {"max_retries": max_retries, "timeout": timeout}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    @property
    def name(self) -> str:
        return "openai"

    def build_request(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        max_tokens: int,
        system: str | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        spec = spec_for(model, provider="openai")
        payload: list[dict[str, str]] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend({"role": m.role, "content": m.content} for m in messages)

        params: dict[str, Any] = {
            "model": spec.model_id,
            "messages": payload,
            "max_completion_tokens": min(max_tokens, spec.max_output or max_tokens),
        }
        if temperature is not None and spec.supports_sampling:
            params["temperature"] = temperature
        return params

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
        params = self.build_request(
            messages,
            model=model,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
        )
        client: Any = (
            self._client if timeout is None else self._client.with_options(timeout=timeout)
        )
        try:
            response = await client.chat.completions.create(**params)
        except openai.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"openai connection error: {exc}") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"openai {exc.status_code}: {exc}") from exc

        choice = response.choices[0]
        usage_obj = response.usage
        usage = Usage(
            input_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
        )
        spec = spec_for(response.model, provider="openai")
        return Completion(
            text=choice.message.content or "",
            model=response.model,
            provider="openai",
            usage=usage,
            stop_reason=_normalise_stop(choice.finish_reason),
            cost_usd=spec.cost_usd(usage.input_tokens, usage.output_tokens),
            raw=response,
        )

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
        params = self.build_request(
            messages,
            model=model,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
        )
        client: Any = (
            self._client if timeout is None else self._client.with_options(timeout=timeout)
        )
        try:
            stream = await client.chat.completions.create(**params, stream=True)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except openai.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc)) from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"openai {exc.status_code}: {exc}") from exc

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        """No counting endpoint here, so this is an explicit approximation.

        It is labelled as such rather than dressed up: ~4 characters per token
        plus per-message overhead. Anything that needs an exact count should be
        routed to a provider that offers one.
        """
        chars = sum(len(m.content) for m in messages) + len(system or "")
        return chars // 4 + 4 * (len(messages) + (1 if system else 0))

    async def transcribe(
        self, audio: bytes, *, model: str, filename: str = "audio.ogg", language: str | None = None
    ) -> Transcript:
        start = time.perf_counter()
        try:
            result = await self._client.audio.transcriptions.create(
                model=model,
                file=(filename, audio),
                language=language,  # type: ignore[arg-type]
            )
        except openai.RateLimitError as exc:
            raise LLMRateLimitedError(str(exc)) from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"openai transcribe {exc.status_code}: {exc}") from exc

        return Transcript(
            text=getattr(result, "text", ""),
            model=model,
            provider="openai",
            language=language,
            duration_seconds=time.perf_counter() - start,
        )

    async def close(self) -> None:
        await self._client.close()


def _normalise_stop(value: str | None) -> StopReason:
    match value:
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
