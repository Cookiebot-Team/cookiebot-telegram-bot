"""Cost of the call itself: one float in, one bool out, no work inside."""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import cb_hot
from cy import noopmod as cy_noop
from py import noopmod as py_noop

ITERS = 1_000_000
ROUNDS = 7


def bench(fn) -> float:
    best = float("inf")
    for _ in range(ROUNDS):
        start = time.perf_counter_ns()
        for i in range(ITERS):
            fn(1.0)
        best = min(best, (time.perf_counter_ns() - start) / ITERS)
    return best


def empty(_v: float) -> bool:
    return True


targets = {
    "python loop only": empty,
    "python method": py_noop.Noop().noop,
    "cython cclass": cy_noop.Noop().noop,
    "mojo binding": cb_hot.TokenBucket(capacity=1.0, rate=1.0).noop,
}
print(f"{'call':<20} {'ns/call':>9}")
for label, fn in targets.items():
    print(f"{label:<20} {bench(fn):>9.1f}")
