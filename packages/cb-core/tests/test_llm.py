"""Unit tests for the LLM layer.

The important ones are the parameter-gating tests: current Claude models return a
400 for `temperature`, and reject disabled thinking above `high` effort. A generic
wrapper that forwards whatever it was handed breaks on the default model, so the
filtering is tested directly rather than through a live call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from importlib import import_module
from typing import Any

import pytest

from cb_core import db as db_mod
from cb_core import tenancy as tenancy_mod
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
from cb_core.llm.base import LLMProvider
from cb_core.llm.catalog import CATALOG, ModelSpec
from cb_core.llm.router import _Breaker
from cb_core.llm.types import Completion, Transcript
from cb_core.tenancy import Tenant

# `from cb_core.llm import router` (and even `import cb_core.llm.router as x`,
# which still resolves through the package's own namespace) both give the
# `router()` factory function, not the submodule: `cb_core/llm/__init__.py`'s
# own `from cb_core.llm.router import (..., router, ...)` rebinds the package
# attribute `cb_core.llm.router` to that function, shadowing the submodule.
# `import_module` goes straight to `sys.modules`, bypassing the shadowed
# attribute, which is what `log.warning` needs to be monkeypatched below.
router_mod = import_module("cb_core.llm.router")


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


class _TranscribeStubProvider(_StubProvider):
    """Like `_StubProvider`, but `transcribe` actually returns something —
    the base class's raises `NotImplementedError` since no existing test
    exercised it."""

    def __init__(
        self,
        transcript: Transcript,
        *,
        delay: float = 0.0,
        fail_times: int = 0,
    ) -> None:
        super().__init__(_completion())
        self.transcript = transcript
        self.delay = delay
        self.fail_times = fail_times
        self.transcribe_calls: list[dict[str, object]] = []

    async def transcribe(
        self,
        audio: bytes,
        *,
        model: str,
        filename: str = "a.ogg",
        language: str | None = None,
    ) -> Transcript:
        self.transcribe_calls.append({"model": model, "filename": filename, "language": language})
        if self.delay:
            await asyncio.sleep(self.delay)
        if len(self.transcribe_calls) <= self.fail_times:
            raise LLMError("boom")
        return self.transcript


def _transcript(**kw: object) -> Transcript:
    base: dict[str, object] = {"text": "hello", "model": "stub-model", "provider": "stub"}
    base.update(kw)
    return Transcript(**base)


class TestRouterTranscribe:
    """R3.1-R3.4: `transcribe` hardened the same way `complete` already is.

    R3.3 (tenant budget) is covered end-to-end in `test_llm_budget.py`; these
    tests cover the other three gaps `complete` closed and `transcribe` did not:
    a timeout, a metered `llm_usage` row, and a shared circuit breaker.
    """

    def _router(
        self, provider: _TranscribeStubProvider, *, timeout: float | None = 60.0
    ) -> LLMRouter:
        return LLMRouter(
            {"stub": provider},
            {"transcribe": TaskConfig(provider="stub", model="stub-model", timeout=timeout)},
            record_usage=False,
        )

    async def test_task_config_drives_the_call(self) -> None:
        provider = _TranscribeStubProvider(_transcript())
        router = self._router(provider)
        result = await router.transcribe(b"audio", filename="voice.ogg", language="pt")
        assert result.text == "hello"
        assert provider.transcribe_calls[0]["model"] == "stub-model"
        assert provider.transcribe_calls[0]["filename"] == "voice.ogg"
        assert provider.transcribe_calls[0]["language"] == "pt"

    async def test_slow_provider_raises_rather_than_hanging(self) -> None:
        """R3.1 / D-ST-2: v1 set no timeout at all on this call. A provider that
        never returns must not pin the caller forever."""
        provider = _TranscribeStubProvider(_transcript(), delay=10.0)
        router = self._router(provider, timeout=0.05)
        with pytest.raises(LLMError):
            await router.transcribe(b"audio")

    async def test_usage_row_written_when_group_id_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R3.2: transcription spend must stop being invisible in `llm_usage` —
        with a null `cost_usd`, since `Transcript` carries no price and whisper
        is not in `catalog.py` (HANDOFF §6.3: no number is to be guessed)."""
        calls: list[tuple[str, tuple[Any, ...]]] = []

        async def fake_execute(stmt: str, *args: Any, name: str = "execute") -> str:
            calls.append((name, args))
            return "INSERT 0 1"

        monkeypatch.setattr(db_mod, "execute", fake_execute)

        provider = _TranscribeStubProvider(_transcript(model="whisper-1", provider="openai"))
        router = LLMRouter(
            {"stub": provider},
            {"transcribe": TaskConfig(provider="stub", model="whisper-1")},
        )
        await router.transcribe(b"audio", group_id=42, user_id=7)

        assert len(calls) == 1
        name, args = calls[0]
        assert name == "llm_usage_insert"
        (
            usage_id,
            group_id,
            user_id,
            task,
            provider_col,
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cost_usd,
            latency_ms,
            outcome,
            _trace_id,
        ) = args
        assert is_uuid7(usage_id)
        assert (group_id, user_id, task) == (42, 7, "transcribe")
        assert (provider_col, model) == ("openai", "whisper-1")
        assert (input_tokens, output_tokens, cache_read_tokens) == (0, 0, 0)
        assert cost_usd is None
        assert isinstance(latency_ms, int) and latency_ms >= 0
        assert outcome == "ok"

    async def test_no_usage_row_without_group_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_execute(stmt: str, *args: Any, name: str = "execute") -> str:
            raise AssertionError("no group_id means no persist call")

        monkeypatch.setattr(db_mod, "execute", fake_execute)

        provider = _TranscribeStubProvider(_transcript())
        router = LLMRouter(
            {"stub": provider}, {"transcribe": TaskConfig(provider="stub", model="stub-model")}
        )
        result = await router.transcribe(b"audio")
        assert result.text == "hello"

    async def test_circuit_opens_after_repeated_failures(self) -> None:
        """R3.4: `transcribe` shares `complete`'s per-provider breaker."""
        provider = _TranscribeStubProvider(_transcript(), fail_times=10)
        router = self._router(provider)
        for _ in range(5):
            with pytest.raises(LLMError):
                await router.transcribe(b"audio")
        with pytest.raises(LLMUnavailableError):
            await router.transcribe(b"audio")
        # the open breaker short-circuits before the provider is asked again
        assert len(provider.transcribe_calls) == 5

    async def test_open_breaker_is_shared_with_complete(self) -> None:
        """`complete` and `transcribe` key `_breakers` by `cfg.provider`, not by
        task — a provider tripped by one task is unavailable to the other."""

        class Failing(_TranscribeStubProvider):
            async def complete(self, messages: Sequence[Message], **kwargs: object) -> Completion:
                raise LLMError("boom")

        provider = Failing(_transcript())
        router = LLMRouter(
            {"stub": provider},
            {
                "chat": TaskConfig(provider="stub", model="stub-model"),
                "transcribe": TaskConfig(provider="stub", model="stub-model"),
            },
            record_usage=False,
        )
        for _ in range(5):
            with pytest.raises(LLMError):
                await router.complete("chat", [Message(role="user", content="x")])
        with pytest.raises(LLMUnavailableError):
            await router.transcribe(b"audio")


def _install_tenant(monkeypatch: pytest.MonkeyPatch, tenant: Tenant) -> None:
    """Same seam `test_llm_budget.py`'s `install_tenant` patches, but router.py
    reaches `tenancy.registry` through a local import rather than a module-level
    one (see `complete`'s comment on why), so the patch target here is the
    singleton itself, not a name inside `cb_core.llm.router`."""

    async def fake_by_id(tenant_id: str) -> Tenant:
        assert tenant_id == tenant.tenant_id
        return tenant

    monkeypatch.setattr(tenancy_mod.registry, "by_id", fake_by_id)


class TestRouterTenantOverrides:
    """`Tenant.llm_overrides`: per-tenant task -> model overrides, merged over
    the global `TaskConfig` (`router.py`'s `_merge_task_config`/`config_for`).
    """

    def _router(self, provider: _StubProvider, **extra_providers: LLMProvider) -> LLMRouter:
        providers: dict[str, LLMProvider] = {"stub": provider, **extra_providers}
        return LLMRouter(
            providers,
            {
                "chat": TaskConfig(
                    provider="stub", model="stub-model", max_tokens=64, effort="low", system="base"
                )
            },
            record_usage=False,
        )

    def test_config_for_with_no_tenant_is_unchanged(self) -> None:
        router = self._router(_StubProvider(_completion()))
        assert router.config_for("chat") == TaskConfig(
            provider="stub", model="stub-model", max_tokens=64, effort="low", system="base"
        )

    def test_override_merges_field_by_field(self) -> None:
        """A tenant naming only `model` keeps every other global field —
        `max_tokens`/`effort`/`system` here — rather than being forced to
        restate a whole `TaskConfig`."""
        router = self._router(_StubProvider(_completion()))
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            llm_overrides={"chat": {"model": "stub-model-2"}},
        )
        merged = router.config_for("chat", tenant=tenant)
        assert merged.model == "stub-model-2"
        assert merged.max_tokens == 64
        assert merged.effort == "low"
        assert merged.system == "base"

    def test_tenant_with_no_override_for_this_task_is_unaffected(self) -> None:
        router = self._router(_StubProvider(_completion()))
        tenant = Tenant(tenant_id="acme", display_name="Acme", llm_overrides={})
        assert router.config_for("chat", tenant=tenant) == router.config_for("chat")

    async def test_no_tenant_id_never_resolves_a_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The registry lookup is the seam this test guards: `tenant_id=None`
        must skip it entirely, not just skip the override merge."""

        async def boom(tenant_id: str) -> Tenant:
            raise AssertionError("tenant_id=None must never call TenantRegistry.by_id")

        monkeypatch.setattr(tenancy_mod.registry, "by_id", boom)

        provider = _StubProvider(_completion())
        result = await self._router(provider).complete("chat", [Message(role="user", content="hi")])
        assert result.text == "ok"
        assert provider.calls[0]["model"] == "stub-model"

    async def test_tenant_id_resolves_the_registry_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`config_for`'s override merge and `ensure_within_budget` both need the
        tenant row; `complete()` must fetch it once and hand the same object to
        both rather than paying `by_id` twice for one completion."""
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            llm_overrides={"chat": {"model": "stub-model-2"}},
        )
        calls = 0

        async def fake_by_id(tenant_id: str) -> Tenant:
            nonlocal calls
            calls += 1
            return tenant

        monkeypatch.setattr(tenancy_mod.registry, "by_id", fake_by_id)

        provider = _StubProvider(_completion())
        result = await self._router(provider).complete(
            "chat", [Message(role="user", content="hi")], tenant_id="acme"
        )
        assert result.text == "ok"
        assert calls == 1
        assert provider.calls[0]["model"] == "stub-model-2"

    async def test_override_names_unconfigured_provider_falls_back_and_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            llm_overrides={"chat": {"provider": "ghost", "model": "ghost-model"}},
        )
        _install_tenant(monkeypatch, tenant)
        events: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            router_mod.log, "warning", lambda event, **kw: events.append((event, kw))
        )

        provider = _StubProvider(_completion())
        result = await self._router(provider).complete(
            "chat", [Message(role="user", content="hi")], tenant_id="acme"
        )

        assert result.text == "ok"
        assert provider.calls[0]["model"] == "stub-model"  # fell back to the global config
        assert events == [
            (
                "llm.tenant_override_unconfigured_provider",
                {"tenant_id": "acme", "task": "chat", "provider": "ghost"},
            )
        ]

    async def test_unparseable_override_falls_back_and_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A field name that is not a real `TaskConfig` field is exactly as
        unparseable as a value of the wrong shape — both come back from
        `structs.replace` as a `TypeError` and both must not cost the tenant the
        call."""
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            llm_overrides={"chat": {"not_a_real_field": "x"}},
        )
        _install_tenant(monkeypatch, tenant)
        events: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            router_mod.log, "warning", lambda event, **kw: events.append((event, kw))
        )

        provider = _StubProvider(_completion())
        result = await self._router(provider).complete(
            "chat", [Message(role="user", content="hi")], tenant_id="acme"
        )

        assert result.text == "ok"
        assert provider.calls[0]["model"] == "stub-model"
        assert len(events) == 1
        assert events[0][0] == "llm.tenant_override_invalid"
        assert events[0][1]["tenant_id"] == "acme"

    def test_override_for_a_task_that_does_not_exist_globally_raises_like_today(self) -> None:
        """A tenant cannot invent a task the global router never defined —
        `config_for` looks the *requested* task up in `self._tasks` before it
        ever consults `tenant.llm_overrides`, so this is unchanged from the
        no-tenant case (`TestRouter.test_unknown_task_is_an_error`)."""
        router = self._router(_StubProvider(_completion()))
        tenant = Tenant(
            tenant_id="acme", display_name="Acme", llm_overrides={"ghost_task": {"model": "x"}}
        )
        with pytest.raises(LLMError):
            router.config_for("ghost_task", tenant=tenant)

    async def test_override_for_a_task_nobody_requests_is_inert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse of the above: an override keyed to a task the tenant made
        up is simply never looked at, because nothing ever asks the router to
        resolve that task. It costs nothing and breaks nothing."""
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            llm_overrides={"a_task_nobody_asks_for": {"model": "x"}},
        )
        _install_tenant(monkeypatch, tenant)

        provider = _StubProvider(_completion())
        result = await self._router(provider).complete(
            "chat", [Message(role="user", content="hi")], tenant_id="acme"
        )
        assert result.text == "ok"
        assert provider.calls[0]["model"] == "stub-model"

    def test_provider_for_reflects_the_merged_config(self) -> None:
        stub2 = _StubProvider(_completion())
        router = self._router(_StubProvider(_completion()), stub2=stub2)
        tenant = Tenant(
            tenant_id="acme", display_name="Acme", llm_overrides={"chat": {"provider": "stub2"}}
        )
        assert router.provider_for("chat", tenant=tenant) is stub2

    async def test_transcribe_also_resolves_and_merges_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`config_for`/`provider_for` are shared by `complete` and `transcribe`
        (AGENTS.md's "same predicate, no drifting apart" — see the module's own
        precedent in `tenancy.py`'s docstring for `by_skin`), so the merge must
        apply to a transcription task's config the same way."""
        tenant = Tenant(
            tenant_id="acme",
            display_name="Acme",
            llm_overrides={"transcribe": {"filename": "ignored"}},  # not a real field, see below
        )
        _install_tenant(monkeypatch, tenant)

        provider = _TranscribeStubProvider(_transcript(model="whisper-2"))
        router = LLMRouter(
            {"stub": provider},
            {"transcribe": TaskConfig(provider="stub", model="stub-model")},
            record_usage=False,
        )
        # An invalid override (unknown field) must fall back rather than break
        # transcription entirely — the same contract `complete()` gets.
        result = await router.transcribe(b"audio", tenant_id="acme")
        assert result.text == "hello"
        assert provider.transcribe_calls[0]["model"] == "stub-model"


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
