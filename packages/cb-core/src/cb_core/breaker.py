"""Circuit breaker for outbound calls.

v1 called cas.chat, SauceNao, Shazam and OpenAI inline, with no timeout and no
breaker (FEATURE-MAP §5), so one slow dependency consumed threads out of a fixed
50-thread pool until a supervisor script killed the process. Here a failing
dependency is skipped fast and retried after a cooldown.

Lives in `cb_core` rather than inside the LLM router because every outbound
integration needs it — the doomlist's cas.chat and burrbot lookups just as much
as a model provider. One implementation, one set of semantics, one place to fix.
"""

from __future__ import annotations


class Breaker:
    """Closed -> open after `threshold` consecutive failures, half-open after `cooldown`.

    The caller supplies `now` rather than the class reading a clock, so tests
    drive it deterministically and callers that already have a timestamp do not
    pay for a second syscall.
    """

    __slots__ = ("_failures", "_opened_at", "cooldown", "threshold")

    def __init__(self, threshold: int = 5, cooldown: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0

    def allow(self, now: float) -> bool:
        if self._failures < self.threshold:
            return True
        if now - self._opened_at >= self.cooldown:
            self._failures = self.threshold - 1  # half-open: let one through
            return True
        return False

    def record(self, ok: bool, now: float) -> None:
        if ok:
            self._failures = 0
        else:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = now

    @property
    def is_open(self) -> bool:
        return self._failures >= self.threshold
