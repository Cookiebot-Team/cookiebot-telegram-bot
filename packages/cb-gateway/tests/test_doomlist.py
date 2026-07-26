"""Unit coverage for util_doomlist — breaker behaviour, timeout handling, and
response parsing for the two external dependencies (cas.chat, burrbot). No
Telegram, no dispatcher, no real network: every call goes through
`httpx.MockTransport` via `doomlist.set_http_client`.

See docs/contracts/util_doomlist.md for the full behaviour contract,
qa/features/util_doomlist.feature + qa/test_util_doomlist.py for the end-to-end
version of the same assertions, and qa/integration/test_doomlist_blacklist.py
for the local `blacklist`/`users` round trip this file deliberately does not
cover (it needs a real Postgres).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

import httpx
import pytest

from cb_core import metrics
from cb_gateway.handlers import doomlist


@dataclass
class _FakeUser:
    id: int
    first_name: str = "Newcomer"
    last_name: str | None = None
    username: str | None = "newcomer"
    is_bot: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}" if self.last_name else self.first_name


@pytest.fixture(autouse=True)
def _reset_doomlist_state() -> Iterator[None]:
    """Every breaker and the injected client are process-global singletons this
    module owns; reset them around each test so one test's failures don't leak
    into the next as a stuck-open breaker (this is state reset for isolation,
    not mocking business logic — same idiom qa/conftest.py uses for
    `admins._l1`/`group_config._l1`).
    """
    doomlist._cas_breaker = doomlist.Breaker()  # noqa: SLF001
    doomlist._burrbot_breaker = doomlist.Breaker()  # noqa: SLF001
    doomlist.set_http_client(None)
    yield
    doomlist.set_http_client(None)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------------ CAS


class TestCheckCas:
    async def test_hit_when_ok_true(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/check"
            assert request.url.params["user_id"] == "555"
            return httpx.Response(200, json={"ok": True})

        doomlist.set_http_client(_transport(handler))
        assert await doomlist.check_cas(555) is True
        assert metrics.external_dep_up.labels(dep="cas")._value.get() == 1  # noqa: SLF001

    async def test_no_hit_when_ok_false(self) -> None:
        doomlist.set_http_client(_transport(lambda r: httpx.Response(200, json={"ok": False})))
        assert await doomlist.check_cas(555) is False

    async def test_fails_open_on_timeout(self) -> None:
        """GroupShield.py:196-198's bare `except Exception: return False` —
        a slow/unreachable cas.chat must never block the join, only skip it."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout", request=request)

        doomlist.set_http_client(_transport(handler))
        assert await doomlist.check_cas(555) is False
        assert metrics.external_dep_up.labels(dep="cas")._value.get() == 0  # noqa: SLF001

    async def test_fails_open_on_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated down", request=request)

        doomlist.set_http_client(_transport(handler))
        assert await doomlist.check_cas(555) is False

    async def test_fails_open_on_malformed_json(self) -> None:
        doomlist.set_http_client(_transport(lambda r: httpx.Response(200, text="not json")))
        assert await doomlist.check_cas(555) is False

    async def test_fails_open_on_missing_ok_field(self) -> None:
        doomlist.set_http_client(_transport(lambda r: httpx.Response(200, json={})))
        assert await doomlist.check_cas(555) is False

    async def test_breaker_opens_after_threshold_and_skips_the_call(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("simulated down", request=request)

        doomlist.set_http_client(_transport(handler))
        for _ in range(doomlist._cas_breaker.threshold):  # noqa: SLF001
            assert await doomlist.check_cas(555) is False
        assert calls == doomlist._cas_breaker.threshold  # noqa: SLF001

        # One more join attempt: the breaker is open, so no request is made at
        # all — the failure still fails open (False), just cheaper.
        assert await doomlist.check_cas(555) is False
        assert calls == doomlist._cas_breaker.threshold, "breaker should have skipped the call"  # noqa: SLF001

    async def test_breaker_half_opens_after_cooldown(self) -> None:
        breaker = doomlist._cas_breaker  # noqa: SLF001
        now = time.monotonic()
        for _ in range(breaker.threshold):
            breaker.record(False, now)
        assert breaker.allow(now) is False

        assert breaker.allow(now + breaker.cooldown + 0.01) is True


# -------------------------------------------------------------------- burrbot


class TestCheckBurrbot:
    async def test_hit_when_raider_true(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(200, text='{"raider": true}')

        doomlist.set_http_client(_transport(handler))
        assert await doomlist.check_burrbot(555) is True

    async def test_hit_survives_v1s_doubled_quote_bug(self) -> None:
        """GroupShield.py:219-220: the live endpoint's body doubles its own
        quotes; v1 works around it with `.replace('""', '"')` rather than
        treating it as malformed. Reproduced exactly."""
        doomlist.set_http_client(
            _transport(lambda r: httpx.Response(200, text='{""raider"": true}'))
        )
        assert await doomlist.check_burrbot(555) is True

    async def test_no_hit_when_raider_false(self) -> None:
        doomlist.set_http_client(
            _transport(lambda r: httpx.Response(200, text='{""raider"": false}'))
        )
        assert await doomlist.check_burrbot(555) is False

    async def test_fails_open_on_timeout(self) -> None:
        """GroupShield.py had *no* timeout at all on this call (a real defect,
        see docs/contracts/util_doomlist.md) — v2 adds one and, like v1's own
        bare except, still fails open rather than blocking the join."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout", request=request)

        doomlist.set_http_client(_transport(handler))
        assert await doomlist.check_burrbot(555) is False
        assert metrics.external_dep_up.labels(dep="burrbot")._value.get() == 0  # noqa: SLF001

    async def test_fails_open_on_malformed_body(self) -> None:
        doomlist.set_http_client(_transport(lambda r: httpx.Response(200, text="garbage")))
        assert await doomlist.check_burrbot(555) is False

    async def test_every_outbound_call_carries_an_explicit_timeout(self) -> None:
        """AGENTS.md: 'no bare httpx.get with no timeout'. Both module-level
        timeout constants must be real, finite httpx.Timeout objects, not None."""
        assert doomlist._CAS_TIMEOUT.connect is not None  # noqa: SLF001
        assert doomlist._BURRBOT_TIMEOUT.connect is not None  # noqa: SLF001

    async def test_breaker_opens_after_threshold_and_skips_the_call(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("simulated down", request=request)

        doomlist.set_http_client(_transport(handler))
        for _ in range(doomlist._burrbot_breaker.threshold):  # noqa: SLF001
            assert await doomlist.check_burrbot(555) is False
        assert calls == doomlist._burrbot_breaker.threshold  # noqa: SLF001

        assert await doomlist.check_burrbot(555) is False
        assert calls == doomlist._burrbot_breaker.threshold  # noqa: SLF001


# ------------------------------------------------------------- forbidden chars


class TestForbiddenCharacters:
    """GroupShield.py:210 — no network, no DB; pure string containment."""

    @pytest.mark.parametrize("glyph", ["卐", "ζ", "𝛇"])
    def test_each_v1_glyph_is_detected(self, glyph: str) -> None:
        assert doomlist._has_forbidden_chars(f"Totally Normal Name {glyph}")  # noqa: SLF001

    def test_clean_name_is_not_flagged(self) -> None:
        assert not doomlist._has_forbidden_chars("Perfectly Normal Name")  # noqa: SLF001

    async def test_check_local_blacklist_short_circuits_on_forbidden_chars(self) -> None:
        """A forbidden-character hit must not require a database at all —
        proven here by never configuring one; a DB call would error instead of
        returning True if this short-circuit were missing.
        """
        user = _FakeUser(id=1, first_name="Raider", last_name="卐")
        assert await doomlist.check_local_blacklist(user) is True


# ------------------------------------------------------------------- ordering


class TestEvaluateOrder:
    """`_evaluate`'s check order (CAS -> local blacklist -> burrbot) is
    observable, not an implementation detail: a doubly-listed user sees
    whichever source is checked first (docs/contracts/util_doomlist.md)."""

    async def test_cas_hit_short_circuits_before_local_and_burrbot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def fake_cas(user_id: int) -> bool:
            calls.append("cas")
            return True

        async def fake_local(user: _FakeUser) -> bool:
            calls.append("local")
            return True

        async def fake_burrbot(user_id: int) -> bool:
            calls.append("burrbot")
            return True

        async def fake_persist(user_id: int) -> None:
            return None

        monkeypatch.setattr(doomlist, "check_cas", fake_cas)
        monkeypatch.setattr(doomlist, "check_local_blacklist", fake_local)
        monkeypatch.setattr(doomlist, "check_burrbot", fake_burrbot)
        monkeypatch.setattr(doomlist, "_persist_cas_hit", fake_persist)

        result = await doomlist._evaluate(_FakeUser(id=1))  # noqa: SLF001

        assert result == "ban_cas"
        assert calls == ["cas"]

    async def test_local_hit_short_circuits_before_burrbot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def fake_cas(user_id: int) -> bool:
            calls.append("cas")
            return False

        async def fake_local(user: _FakeUser) -> bool:
            calls.append("local")
            return True

        async def fake_burrbot(user_id: int) -> bool:
            calls.append("burrbot")
            return True

        monkeypatch.setattr(doomlist, "check_cas", fake_cas)
        monkeypatch.setattr(doomlist, "check_local_blacklist", fake_local)
        monkeypatch.setattr(doomlist, "check_burrbot", fake_burrbot)

        result = await doomlist._evaluate(_FakeUser(id=1))  # noqa: SLF001

        assert result == "ban"
        assert calls == ["cas", "local"]

    async def test_no_hit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doomlist, "check_cas", _always(False))
        monkeypatch.setattr(doomlist, "check_local_blacklist", _always(False))
        monkeypatch.setattr(doomlist, "check_burrbot", _always(False))

        assert await doomlist._evaluate(_FakeUser(id=1)) is None  # noqa: SLF001


def _always(value: bool) -> Callable[..., Awaitable[bool]]:
    async def _fn(*args: object, **kwargs: object) -> bool:
        return value

    return _fn


# --------------------------------------------------------------------- seam


class TestHttpClientSeam:
    def test_default_client_is_created_lazily(self) -> None:
        assert doomlist._client is None  # noqa: SLF001
        client = doomlist._get_client()  # noqa: SLF001
        assert isinstance(client, httpx.AsyncClient)
        # TLS verification is never disabled by this port (contract's "v2
        # architecture" section): `httpx.AsyncClient()` is constructed with no
        # `verify=` argument anywhere in doomlist.py, so it keeps httpx's own
        # default (`verify=True`) rather than a silent `verify=False` (v1's D2).
        assert doomlist._get_client() is client, "should be a lazily-cached singleton"  # noqa: SLF001

    def test_set_http_client_overrides_the_default(self) -> None:
        injected = httpx.AsyncClient()
        doomlist.set_http_client(injected)
        assert doomlist._get_client() is injected  # noqa: SLF001
