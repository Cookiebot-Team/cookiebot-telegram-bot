"""Unit coverage for `cb_gateway.queue` — the gateway->worker enqueue wiring
`handlers/calladms.py` and `handlers/groupguardian.py` both say does not exist
yet (docs/site/content/docs/architecture.mdx §2, `.specs/features/util_everyone/design.md` R2).

No real Redis: `_get_pool` is monkeypatched to a fake `ArqRedis`-shaped object,
so this only exercises `enqueue`/`close`'s own logic — accept, fail, count,
never raise. Model: `test_stickerspam.py`'s `boom` fixture for "the broker is
down" and `packages/cb-core/tests/test_admins.py`'s `spy_labels` for asserting
a counter without a live Prometheus registry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from prometheus_client import Counter

from cb_gateway import queue


class _FakePool:
    """Stands in for `arq.connections.ArqRedis`: only `enqueue_job` and `aclose`
    are ever called on the real thing."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.closed = False

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(((function, *args), kwargs))
        if self.raises is not None:
            raise self.raises

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_pool() -> Iterator[None]:
    """`_pool` is module-global and lazily created; every test starts clean."""
    queue._pool = None  # noqa: SLF001 - test owns this module-level seam
    yield
    queue._pool = None  # noqa: SLF001


class TestEnqueueAccepts:
    async def test_returns_true_and_forwards_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _FakePool()
        monkeypatch.setattr(queue, "_get_pool", _fake_get_pool(pool))

        accepted = await queue.enqueue("everyone_fanout", group_id=-100, chat_title="Cookies")

        assert accepted is True
        assert pool.calls == [(("everyone_fanout",), {"group_id": -100, "chat_title": "Cookies"})]

    async def test_counts_the_ok_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _FakePool()
        monkeypatch.setattr(queue, "_get_pool", _fake_get_pool(pool))
        seen: list[tuple[str, str]] = []
        real_labels = queue.enqueue_total.labels

        def spy_labels(*, job: str, outcome: str) -> Counter:
            seen.append((job, outcome))
            return real_labels(job=job, outcome=outcome)

        monkeypatch.setattr(queue.enqueue_total, "labels", spy_labels)

        await queue.enqueue("everyone_fanout")

        assert ("everyone_fanout", "ok") in seen


class TestEnqueueNeverRaises:
    async def test_pool_raising_returns_false_and_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _FakePool(raises=ConnectionError("valkey unreachable"))
        monkeypatch.setattr(queue, "_get_pool", _fake_get_pool(pool))
        logged: list[dict[str, Any]] = []
        monkeypatch.setattr(
            queue.log, "warning", lambda event, **kw: logged.append({"event": event, **kw})
        )

        accepted = await queue.enqueue("everyone_fanout", group_id=-100)

        assert accepted is False
        assert logged == [
            {"event": "queue.enqueue", "job": "everyone_fanout", "error": "valkey unreachable"}
        ]

    async def test_pool_raising_counts_the_error_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _FakePool(raises=RuntimeError("boom"))
        monkeypatch.setattr(queue, "_get_pool", _fake_get_pool(pool))
        seen: list[tuple[str, str]] = []
        real_labels = queue.enqueue_total.labels

        def spy_labels(*, job: str, outcome: str) -> Counter:
            seen.append((job, outcome))
            return real_labels(job=job, outcome=outcome)

        monkeypatch.setattr(queue.enqueue_total, "labels", spy_labels)

        await queue.enqueue("everyone_fanout")

        assert ("everyone_fanout", "error") in seen

    async def test_pool_construction_failure_also_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom() -> _FakePool:
            raise OSError("cannot reach valkey")

        monkeypatch.setattr(queue, "_get_pool", boom)

        accepted = await queue.enqueue("everyone_fanout")

        assert accepted is False


class TestClose:
    async def test_close_clears_the_pool_and_is_idempotent(self) -> None:
        fake = _FakePool()
        queue._pool = fake  # noqa: SLF001 - simulating an already-open pool

        await queue.close()
        assert queue._pool is None  # noqa: SLF001
        assert fake.closed is True

        # A second call must not blow up on a pool that is already gone.
        await queue.close()
        assert queue._pool is None  # noqa: SLF001


def _fake_get_pool(pool: _FakePool) -> Callable[[], Awaitable[_FakePool]]:
    async def _get() -> _FakePool:
        return pool

    return _get
