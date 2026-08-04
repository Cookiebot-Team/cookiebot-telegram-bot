"""Unit tests for the tenant LLM budget cap — no Postgres, no Valkey.

`Tenant.monthly_llm_budget_usd` has existed since `0003_tenants.py:40` and has
never been enforced. Our own cache and db modules are monkeypatched at their
public seams (`cb_core.cache.get_json`/`set_json`, `cb_core.db.fetchrow`), same
convention as `test_admins.py`, per the "don't fake our own code" rule.

The load-bearing behaviour is R2.4's asymmetry: a spend query that *succeeds*
and shows the tenant over budget raises; a cache or database *failure* while
computing the spend fails open instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from cb_core.llm import budget as budget_mod
from cb_core.llm.router import LLMRouter, TaskConfig
from cb_core.llm.types import Completion, LLMBudgetExceededError, Message, Transcript, Usage
from cb_core.tenancy import Tenant

# --------------------------------------------------------------------------- helpers


def make_tenant(budget: float | None) -> Tenant:
    return Tenant(tenant_id="acme", display_name="Acme", monthly_llm_budget_usd=budget)


class FakeCache:
    """Dict-backed stand-in for Valkey's get_json/set_json."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[str] = []

    async def get_json(self, key: str) -> Any | None:
        self.get_calls.append(key)
        return self.store.get(key)

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.set_calls.append(key)
        self.store[key] = value


def make_fetchrow(totals_by_name: dict[str, float]) -> Any:
    """Fakes `db.fetchrow`, keyed on the `name=` kwarg budget.py passes."""

    calls: list[str] = []

    async def fake_fetchrow(*_args: Any, name: str = "fetchrow", **_kwargs: Any) -> dict[str, Any]:
        calls.append(name)
        return {"total": totals_by_name.get(name, 0.0)}

    fake_fetchrow.calls = calls  # type: ignore[attr-defined]
    return fake_fetchrow


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch) -> FakeCache:
    fake = FakeCache()
    monkeypatch.setattr(budget_mod.cache, "get_json", fake.get_json)
    monkeypatch.setattr(budget_mod.cache, "set_json", fake.set_json)
    return fake


def install_tenant(monkeypatch: pytest.MonkeyPatch, tenant: Tenant) -> None:
    async def fake_by_id(tenant_id: str) -> Tenant:
        assert tenant_id == tenant.tenant_id
        return tenant

    monkeypatch.setattr(budget_mod.tenancy.registry, "by_id", fake_by_id)


# --------------------------------------------------------------------------- month_to_date_usd


class TestMonthToDateUsd:
    async def test_sums_rollup_and_todays_live_rows(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 12.5, "llm_budget_today": 2.5})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        total = await budget_mod.month_to_date_usd("acme")

        assert total == pytest.approx(15.0)
        assert set(fetchrow.calls) == {"llm_budget_rolled_up", "llm_budget_today"}

    async def test_result_is_cached(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 1.0, "llm_budget_today": 0.0})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        await budget_mod.month_to_date_usd("acme")
        await budget_mod.month_to_date_usd("acme")

        assert len(fetchrow.calls) == 2, "second call must be served from cache, not re-queried"
        assert fake_cache.store["cb:llm:mtd:acme"] == pytest.approx(1.0)

    async def test_cache_hit_skips_the_database_entirely(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_cache.store["cb:llm:mtd:acme"] = 7.5

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("database must not be queried on a cache hit")

        monkeypatch.setattr(budget_mod.db, "fetchrow", boom)

        assert await budget_mod.month_to_date_usd("acme") == pytest.approx(7.5)


# --------------------------------------------------------------------------- ensure_within_budget


class TestEnsureWithinBudget:
    async def test_under_budget_passes(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_tenant(monkeypatch, make_tenant(budget=100.0))
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 10.0, "llm_budget_today": 5.0})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        await budget_mod.ensure_within_budget("acme")  # must not raise

    async def test_over_budget_raises(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_tenant(monkeypatch, make_tenant(budget=50.0))
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 40.0, "llm_budget_today": 20.0})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        with pytest.raises(LLMBudgetExceededError) as exc_info:
            await budget_mod.ensure_within_budget("acme")

        assert exc_info.value.tenant_id == "acme"
        assert exc_info.value.spent_usd == pytest.approx(60.0)
        assert exc_info.value.budget_usd == pytest.approx(50.0)

    async def test_spend_exactly_at_budget_raises(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap is a ceiling, not a floor: spend == budget means no more room."""
        install_tenant(monkeypatch, make_tenant(budget=10.0))
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 10.0, "llm_budget_today": 0.0})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        with pytest.raises(LLMBudgetExceededError):
            await budget_mod.ensure_within_budget("acme")

    async def test_no_budget_configured_is_never_checked(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_tenant(monkeypatch, make_tenant(budget=None))

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("a tenant with no budget must never be queried")

        monkeypatch.setattr(budget_mod.cache, "get_json", boom)
        monkeypatch.setattr(budget_mod.db, "fetchrow", boom)

        await budget_mod.ensure_within_budget("acme")  # must not raise, must not query

    async def test_cache_failure_fails_open_and_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_tenant(monkeypatch, make_tenant(budget=1.0))

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("cache not initialised; call init_cache() during startup")

        monkeypatch.setattr(budget_mod.cache, "get_json", boom)

        events: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            budget_mod.log, "warning", lambda event, **kw: events.append((event, kw))
        )

        await budget_mod.ensure_within_budget("acme")  # must not raise: fails open

        assert events and events[0][0] == "llm.budget_check_failed"
        assert events[0][1]["tenant_id"] == "acme"

    async def test_database_failure_fails_open_and_counts(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_tenant(monkeypatch, make_tenant(budget=1.0))

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("connection refused")

        monkeypatch.setattr(budget_mod.db, "fetchrow", boom)

        before = budget_mod.metrics.llm_budget_check_failed_total._value.get()  # noqa: SLF001

        await budget_mod.ensure_within_budget("acme")  # must not raise: fails open

        after = budget_mod.metrics.llm_budget_check_failed_total._value.get()  # noqa: SLF001
        assert after == before + 1


# --------------------------------------------------------------------------- router wiring


class _StubProvider:
    """Records what the router asked for; returns a canned completion."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.transcribe_calls: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return "stub"

    async def complete(self, messages: Sequence[Message], **kwargs: object) -> Completion:
        self.calls.append(kwargs)
        return Completion(
            text="ok", model="stub-model", provider="stub", usage=Usage(input_tokens=1)
        )

    async def stream(
        self, messages: Sequence[Message], **kwargs: object
    ) -> AsyncIterator[str]:  # pragma: no cover - unused
        yield ""

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        return 1

    async def transcribe(
        self,
        audio: bytes,
        *,
        model: str,
        filename: str = "a.ogg",
        language: str | None = None,
    ) -> Transcript:
        self.transcribe_calls.append({"model": model, "filename": filename})
        return Transcript(text="hi", model=model, provider="stub")

    async def close(self) -> None:
        return None


def _router(provider: _StubProvider) -> LLMRouter:
    return LLMRouter(
        {"stub": provider},
        {
            "chat": TaskConfig(provider="stub", model="stub-model"),
            "transcribe": TaskConfig(provider="stub", model="stub-model"),
        },
        record_usage=False,
    )


class TestRouterBudgetWiring:
    async def test_tenant_id_none_skips_the_check_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(_tenant_id: str) -> None:
            raise AssertionError("tenant_id=None must never call ensure_within_budget")

        monkeypatch.setattr(budget_mod, "ensure_within_budget", boom)

        provider = _StubProvider()
        result = await _router(provider).complete("chat", [Message(role="user", content="hi")])

        assert result.text == "ok"
        assert len(provider.calls) == 1

    async def test_over_budget_raises_before_any_provider_call(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_tenant(monkeypatch, make_tenant(budget=1.0))
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 5.0, "llm_budget_today": 0.0})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        provider = _StubProvider()
        with pytest.raises(LLMBudgetExceededError):
            await _router(provider).complete(
                "chat", [Message(role="user", content="hi")], tenant_id="acme"
            )

        assert provider.calls == [], "no provider call must be made once over budget"

    async def test_under_budget_reaches_the_provider(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_tenant(monkeypatch, make_tenant(budget=100.0))
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 1.0, "llm_budget_today": 0.0})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        provider = _StubProvider()
        result = await _router(provider).complete(
            "chat", [Message(role="user", content="hi")], tenant_id="acme"
        )

        assert result.text == "ok"
        assert len(provider.calls) == 1

    async def test_transcribe_over_budget_raises_before_any_provider_call(
        self, fake_cache: FakeCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_tenant(monkeypatch, make_tenant(budget=1.0))
        fetchrow = make_fetchrow({"llm_budget_rolled_up": 5.0, "llm_budget_today": 0.0})
        monkeypatch.setattr(budget_mod.db, "fetchrow", fetchrow)

        provider = _StubProvider()
        with pytest.raises(LLMBudgetExceededError):
            await _router(provider).transcribe(b"audio", tenant_id="acme")

        assert provider.transcribe_calls == []

    async def test_transcribe_tenant_id_none_skips_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(_tenant_id: str) -> None:
            raise AssertionError("tenant_id=None must never call ensure_within_budget")

        monkeypatch.setattr(budget_mod, "ensure_within_budget", boom)

        provider = _StubProvider()
        result = await _router(provider).transcribe(b"audio")

        assert result.text == "hi"
        assert len(provider.transcribe_calls) == 1
