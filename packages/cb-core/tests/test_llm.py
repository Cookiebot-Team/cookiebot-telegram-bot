"""Unit tests for the LLM layer.

The important ones are the parameter-gating tests: current Claude models return a
400 for `temperature`, and reject disabled thinking above `high` effort. A generic
wrapper that forwards whatever it was handed breaks on the default model, so the
filtering is tested directly rather than through a live call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from cb_core.ids import is_uuid7, timestamp_ms, uuid7
from cb_core.llm import (
    LLMError,
    LLMRouter,
    LLMUnavailableError,
    Message,
    TaskConfig,
    Usage,
    spec_for,
)
from cb_core.llm.anthropic_provider import AnthropicProvider
from cb_core.llm.catalog import CATALOG, ModelSpec
from cb_core.llm.router import _Breaker
from cb_core.llm.types import Completion, Transcript


class TestCatalog:
    def test_flagship_rejects_sampling(self) -> None:
        # Passing temperature to this model is a 400 — the flag is what stops it.
        assert spec_for("claude-opus-5").supports_sampling is False

    def test_haiku_still_accepts_sampling(self) -> None:
        assert spec_for("claude-haiku-4-5").supports_sampling is True

    def test_haiku_has_no_effort_ladder(self) -> None:
        assert spec_for("claude-haiku-4-5").supports_effort is False

    def test_unknown_model_is_conservative_not_an_error(self) -> None:
        spec = spec_for("some-self-hosted-model", provider="openai")
        assert spec.supports_sampling is False
        assert spec.supports_effort is False
        assert spec.thinking == "none"

    def test_unknown_model_has_no_price(self) -> None:
        assert spec_for("some-self-hosted-model").cost_usd(1000, 1000) is None

    def test_cost_maths(self) -> None:
        spec = CATALOG["claude-opus-5"]
        # 1M input at $5 + 1M output at $25
        assert spec.cost_usd(1_000_000, 1_000_000) == pytest.approx(30.0)

    def test_openai_pricing_is_unset_rather_than_guessed(self) -> None:
        assert CATALOG["gpt-5"].input_usd_per_mtok is None


class TestAnthropicParameterGating:
    @pytest.fixture
    def provider(self) -> AnthropicProvider:
        return AnthropicProvider(api_key="test-key-not-used")

    def test_temperature_dropped_for_models_that_reject_it(
        self, provider: AnthropicProvider
    ) -> None:
        params, _ = provider.build_request(
            [Message(role="user", content="hi")],
            model="claude-opus-5",
            max_tokens=100,
            system=None,
            temperature=0.7,
            effort=None,
            thinking=None,
        )
        assert "temperature" not in params

    def test_temperature_kept_for_models_that_accept_it(self, provider: AnthropicProvider) -> None:
        params, _ = provider.build_request(
            [Message(role="user", content="hi")],
            model="claude-haiku-4-5",
            max_tokens=100,
            system=None,
            temperature=0.3,
            effort=None,
            thinking=None,
        )
        assert params["temperature"] == 0.3

    def test_thinking_on_by_default_for_flagship(self, provider: AnthropicProvider) -> None:
        params, _ = provider.build_request(
            [Message(role="user", content="hi")],
            model="claude-opus-5",
            max_tokens=100,
            system=None,
            temperature=None,
            effort="low",
            thinking=None,
        )
        assert params["thinking"] == {"type": "adaptive"}
        assert params["output_config"] == {"effort": "low"}

    def test_disabled_thinking_caps_effort(self, provider: AnthropicProvider) -> None:
        """`disabled` + xhigh/max is a 400; the provider downgrades instead."""
        params, _ = provider.build_request(
            [Message(role="user", content="hi")],
            model="claude-opus-5",
            max_tokens=100,
            system=None,
            temperature=None,
            effort="xhigh",
            thinking=False,
        )
        assert params["thinking"] == {"type": "disabled"}
        assert params["output_config"] == {"effort": "high"}

    def test_effort_dropped_for_models_without_it(self, provider: AnthropicProvider) -> None:
        params, _ = provider.build_request(
            [Message(role="user", content="hi")],
            model="claude-haiku-4-5",
            max_tokens=100,
            system=None,
            temperature=None,
            effort="high",
            thinking=None,
        )
        assert "output_config" not in params

    def test_max_tokens_clamped_to_model_ceiling(self, provider: AnthropicProvider) -> None:
        params, _ = provider.build_request(
            [Message(role="user", content="hi")],
            model="claude-haiku-4-5",
            max_tokens=999_999,
            system=None,
            temperature=None,
            effort=None,
            thinking=None,
        )
        assert params["max_tokens"] == CATALOG["claude-haiku-4-5"].max_output

    def test_system_messages_folded_into_system_prompt(self, provider: AnthropicProvider) -> None:
        params, _ = provider.build_request(
            [Message(role="system", content="be terse"), Message(role="user", content="hi")],
            model="claude-opus-5",
            max_tokens=100,
            system="base rules",
            temperature=None,
            effort=None,
            thinking=None,
        )
        assert params["system"] == "base rules\n\nbe terse"
        assert [m["role"] for m in params["messages"]] == ["user"]


class TestUsage:
    def test_addition(self) -> None:
        total = Usage(input_tokens=10, output_tokens=5) + Usage(input_tokens=3, output_tokens=1)
        assert (total.input_tokens, total.output_tokens, total.total_tokens) == (13, 6, 19)


class TestBreaker:
    def test_opens_after_threshold(self) -> None:
        b = _Breaker(threshold=3, cooldown=10.0)
        for _ in range(3):
            b.record(False, now=0.0)
        assert not b.allow(now=1.0)

    def test_half_opens_after_cooldown(self) -> None:
        b = _Breaker(threshold=3, cooldown=10.0)
        for _ in range(3):
            b.record(False, now=0.0)
        assert b.allow(now=11.0)

    def test_success_resets(self) -> None:
        b = _Breaker(threshold=2, cooldown=10.0)
        b.record(False, now=0.0)
        b.record(True, now=1.0)
        b.record(False, now=2.0)
        assert b.allow(now=3.0)


class _StubProvider:
    """Records what the router asked for; returns a canned completion."""

    def __init__(self, completion: Completion) -> None:
        self.completion = completion
        self.calls: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return "stub"

    async def complete(self, messages: Sequence[Message], **kwargs: object) -> Completion:
        self.calls.append(kwargs)
        return self.completion

    async def stream(
        self, messages: Sequence[Message], **kwargs: object
    ) -> AsyncIterator[str]:  # pragma: no cover - unused
        yield ""

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        return 42

    async def transcribe(
        self,
        audio: bytes,
        *,
        model: str,
        filename: str = "a.ogg",
        language: str | None = None,
    ) -> Transcript:
        raise NotImplementedError

    async def close(self) -> None:
        return None


def _completion(**kw: object) -> Completion:
    base: dict[str, object] = {
        "text": "ok",
        "model": "stub-model",
        "provider": "stub",
        "usage": Usage(input_tokens=10, output_tokens=5),
    }
    base.update(kw)
    return Completion(**base)


class TestRouter:
    def _router(self, provider: _StubProvider) -> LLMRouter:
        return LLMRouter(
            {"stub": provider},
            {"chat": TaskConfig(provider="stub", model="stub-model", max_tokens=64, effort="low")},
            record_usage=False,
        )

    async def test_task_config_drives_the_call(self) -> None:
        provider = _StubProvider(_completion())
        result = await self._router(provider).complete("chat", [Message(role="user", content="hi")])
        assert result.text == "ok"
        assert provider.calls[0]["model"] == "stub-model"
        assert provider.calls[0]["max_tokens"] == 64
        assert provider.calls[0]["effort"] == "low"

    async def test_unknown_task_is_an_error(self) -> None:
        router = self._router(_StubProvider(_completion()))
        with pytest.raises(LLMError):
            await router.complete("nonexistent", [Message(role="user", content="hi")])

    async def test_unconfigured_provider_is_unavailable(self) -> None:
        router = LLMRouter({}, {"chat": TaskConfig(provider="ghost", model="m")})
        with pytest.raises(LLMUnavailableError):
            await router.complete("chat", [Message(role="user", content="hi")])

    async def test_refusal_is_returned_not_raised(self) -> None:
        """A safety decline is a successful response — callers branch on it."""
        provider = _StubProvider(
            _completion(text="", stop_reason="refusal", refusal_category="cyber")
        )
        result = await self._router(provider).complete("chat", [Message(role="user", content="x")])
        assert result.refused
        assert result.refusal_category == "cyber"

    async def test_circuit_opens_after_repeated_failures(self) -> None:
        class Failing(_StubProvider):
            async def complete(self, messages: Sequence[Message], **kwargs: object) -> Completion:
                raise LLMError("boom")

        router = self._router(Failing(_completion()))
        for _ in range(5):
            with pytest.raises(LLMError):
                await router.complete("chat", [Message(role="user", content="x")])
        with pytest.raises(LLMUnavailableError):
            await router.complete("chat", [Message(role="user", content="x")])

    def test_context_fit_check(self) -> None:
        from cb_core.llm.catalog import register

        register(
            ModelSpec(provider="stub", model_id="stub-model", context_window=1000, max_output=64)
        )
        router = self._router(_StubProvider(_completion()))
        assert router.fits_context("chat", 100, headroom=10)
        assert not router.fits_context("chat", 990, headroom=10)


class TestUuid7:
    def test_version_and_ordering(self) -> None:
        a, b = uuid7(), uuid7()
        assert is_uuid7(a) and is_uuid7(b)
        # v7 sorts by creation time — this is why we use it as a key.
        assert str(a) < str(b) or timestamp_ms(a) <= timestamp_ms(b)

    def test_timestamp_is_recent(self) -> None:
        import time

        now_ms = int(time.time() * 1000)
        assert abs(timestamp_ms(uuid7()) - now_ms) < 5000

    def test_rejects_non_v7(self) -> None:
        import uuid

        with pytest.raises(ValueError):
            timestamp_ms(uuid.uuid4())
