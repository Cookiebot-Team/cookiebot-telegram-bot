"""Rate limiting — runs on literally every update, so it is Cython-compiled.

Pure CPU, no IO, no awaits: the ideal Cython target. Written in Cython's *pure
Python mode* — `@cython.cclass` compiles these into C extension types with C
struct fields, while the same file stays importable and testable as plain Python
when the extension is not built.

Plain type annotations alone were measured to be *slower* than pure Python here
(attribute access still went through the Python object protocol, plus conversion
overhead). Extension types are what actually pays. `python scripts/cb.py bench` is the referee.

Replaces v1 `Bot/Cooldowns.py`, which did check-then-act on unlocked module-level
dicts (`remaining_responses_ai`, `remaining_image_searches`) that were per-process,
never pruned, and reset by a full `.clear()`.
"""

from __future__ import annotations

import cython

COMPILED: bool = cython.compiled


@cython.cclass
class TokenBucket:
    """Classic token bucket. `capacity` burst, refilled at `rate` tokens/second."""

    capacity: cython.double
    rate: cython.double
    _tokens: cython.double
    _last: cython.double

    def __init__(self, capacity: float, rate: float, now: float = 0.0) -> None:
        self.capacity = capacity
        self.rate = rate
        self._tokens = capacity
        self._last = now

    @cython.ccall
    def allow(self, now: cython.double, cost: cython.double = 1.0) -> cython.bint:
        elapsed: cython.double = now - self._last
        tokens: cython.double
        if elapsed > 0.0:
            self._last = now
            tokens = self._tokens + elapsed * self.rate
            self._tokens = tokens if tokens < self.capacity else self.capacity
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    @cython.ccall
    def retry_after(self, cost: cython.double = 1.0) -> cython.double:
        """Seconds until `cost` tokens are available. 0 if available now."""
        deficit: cython.double = cost - self._tokens
        if deficit <= 0.0:
            return 0.0
        if self.rate <= 0.0:
            return float("inf")
        return deficit / self.rate

    @property
    def tokens(self) -> float:
        return self._tokens


@cython.cclass
class SlidingWindow:
    """Bounded sliding-window counter for sticker spam (`core_stickerspam`).

    Older entries fall out of the window instead of accumulating forever the way
    v1's dicts did, and a flood is hard-capped.
    """

    limit: cython.Py_ssize_t
    window: cython.double
    _stamps: list

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._stamps = []

    @cython.ccall
    def hit(self, now: cython.double) -> cython.Py_ssize_t:
        """Record an event; return the count inside the window (post-insert)."""
        cutoff: cython.double = now - self.window
        stamps: list = self._stamps
        drop: cython.Py_ssize_t = 0
        n: cython.Py_ssize_t = len(stamps)
        cap: cython.Py_ssize_t = self.limit * 4
        while drop < n and stamps[drop] <= cutoff:
            drop += 1
        if drop > 0:
            del stamps[:drop]
        stamps.append(now)
        n = len(stamps)
        if n > cap:
            del stamps[: n - cap]
            n = cap
        return n

    @cython.ccall
    def exceeded(self, now: cython.double) -> cython.bint:
        return self.hit(now) > self.limit

    def reset(self) -> None:
        self._stamps.clear()

    @property
    def count(self) -> int:
        return len(self._stamps)


@cython.cclass
class QuotaLedger:
    """Per-key daily quota (AI replies, image searches) with explicit day rollover.

    v1 relied on detecting a date change inside the message handler and then
    clearing a dict; a restart silently refilled everyone's quota.
    """

    limit: cython.Py_ssize_t
    _used: dict
    _day: cython.long

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._used = {}
        self._day = -1

    @cython.ccall
    def take(
        self, key: cython.longlong, day_ordinal: cython.long, cost: cython.Py_ssize_t = 1
    ) -> cython.bint:
        used: cython.Py_ssize_t
        if day_ordinal != self._day:
            self._day = day_ordinal
            self._used.clear()
        used = self._used.get(key, 0)
        if used + cost > self.limit:
            return False
        self._used[key] = used + cost
        return True

    @cython.ccall
    def remaining(self, key: cython.longlong, day_ordinal: cython.long) -> cython.Py_ssize_t:
        if day_ordinal != self._day:
            return self.limit
        return self.limit - self._used.get(key, 0)
