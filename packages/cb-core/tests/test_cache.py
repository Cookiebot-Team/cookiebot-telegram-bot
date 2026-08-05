"""Unit coverage for `cb_core.cache.bump_clamped` — pure logic, no real Valkey.

See `.specs/features/x_conversational_ai/design.md` R4.1: this primitive is
what will hold v1's per-user consecutive-AI-response streak
(`Cooldowns.py:5,24-36`), a signed counter that `incr_window` cannot serve
because it only ever increments and has no seed or clamp. The caller that
spends/replenishes the streak is a later task; this file only exercises the
primitive's own contract.
"""

from __future__ import annotations

import pytest

from cb_core import cache


class FakeScriptClient:
    """Stands in for the Valkey client's `eval()`, replaying the same
    seed/clamp/refresh semantics as `cache._BUMP_CLAMPED_SCRIPT` in pure
    Python. A real Valkey round trip can't be driven through this without a
    live server, so this fake pins the contract directly — same reasoning as
    `FakeWindowCache` in `packages/cb-gateway/tests/test_stickerspam.py`.
    """

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttl_calls: list[tuple[str, int]] = []

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        assert numkeys == 1
        key, delta, ttl_seconds, initial, lo, hi = keys_and_args
        current = self.values.get(key, int(initial))
        value = current + int(delta)
        value = max(int(lo), min(int(hi), value))
        self.values[key] = value
        self.ttl_calls.append((key, int(ttl_seconds)))
        return value


class BoomClient:
    """Simulates an unreachable Valkey — every call raises."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        raise RuntimeError("cache not initialised")


async def _bump_clamped(
    key: str = "cb:ai:streak:1",
    delta: int = -1,
    *,
    lo: int = -7,
    hi: int = 7,
    initial: int = 7,
    ttl_seconds: int = 86400,
) -> int | None:
    return await cache.bump_clamped(
        key, delta, lo=lo, hi=hi, initial=initial, ttl_seconds=ttl_seconds
    )


class TestSeeding:
    @pytest.mark.asyncio
    async def test_missing_key_seeds_to_initial_before_the_delta_is_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeScriptClient()
        monkeypatch.setattr(cache, "client", lambda: fake)

        result = await _bump_clamped(delta=-1, lo=-7, hi=7, initial=7)

        assert result == 6

    @pytest.mark.asyncio
    async def test_existing_key_is_read_not_reseeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeScriptClient()
        fake.values["cb:ai:streak:1"] = 3
        monkeypatch.setattr(cache, "client", lambda: fake)

        result = await _bump_clamped(delta=1, lo=-7, hi=7, initial=7)

        assert result == 4


class TestClamping:
    @pytest.mark.asyncio
    async def test_clamps_at_the_upper_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeScriptClient()
        fake.values["cb:ai:streak:1"] = 7
        monkeypatch.setattr(cache, "client", lambda: fake)

        result = await _bump_clamped(delta=1, lo=-7, hi=7, initial=7)

        assert result == 7

    @pytest.mark.asyncio
    async def test_clamps_at_the_lower_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeScriptClient()
        fake.values["cb:ai:streak:1"] = -7
        monkeypatch.setattr(cache, "client", lambda: fake)

        result = await _bump_clamped(delta=-1, lo=-7, hi=7, initial=7)

        assert result == -7

    @pytest.mark.asyncio
    async def test_a_large_delta_clamps_in_one_step_rather_than_erroring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeScriptClient()
        monkeypatch.setattr(cache, "client", lambda: fake)

        result = await _bump_clamped(delta=100, lo=-7, hi=7, initial=7)

        assert result == 7


class TestTtl:
    @pytest.mark.asyncio
    async def test_ttl_is_refreshed_on_every_call_not_only_the_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeScriptClient()
        monkeypatch.setattr(cache, "client", lambda: fake)

        for _ in range(3):
            await _bump_clamped(delta=-1, lo=-7, hi=7, initial=7, ttl_seconds=86400)

        assert fake.ttl_calls == [("cb:ai:streak:1", 86400)] * 3


class TestFailsOpen:
    @pytest.mark.asyncio
    async def test_a_raising_client_returns_none_instead_of_propagating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cache, "client", lambda: BoomClient())

        result = await _bump_clamped()

        assert result is None
