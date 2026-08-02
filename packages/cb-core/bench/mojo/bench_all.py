"""pure Python vs Cython vs Mojo on the cb_core hot path.

Same measurement method as packages/cb-core/bench/bench_hot.py: best-of-N mean
ns/op, so a noisy sample cannot inflate a win. All three variants run in one
process on the same interpreter (CPython 3.12), so the only difference is the
implementation under the call.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

sys.path.insert(0, ".")

import cb_hot  # noqa: E402
from cy import cooldowns as cy_cooldowns  # noqa: E402
from cy import dedupe as cy_dedupe  # noqa: E402
from cy import textmatch as cy_textmatch  # noqa: E402
from py import cooldowns as py_cooldowns  # noqa: E402
from py import dedupe as py_dedupe  # noqa: E402
from py import textmatch as py_textmatch  # noqa: E402

assert not py_cooldowns.COMPILED, "py/ must stay uncompiled"
assert cy_cooldowns.COMPILED, "cy/ must be compiled"

ROUNDS = 7


def bench(fn: Callable[[], object], iterations: int) -> float:
    timings: list[float] = []
    for _ in range(ROUNDS):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            fn()
        timings.append((time.perf_counter_ns() - start) / iterations)
    return min(timings)


# ---------------------------------------------------------------- cooldowns


def make_cooldowns(mod: object) -> Callable[[], object]:
    bucket = mod.TokenBucket(capacity=20.0, rate=5.0)
    window = mod.SlidingWindow(limit=5, window=10.0)
    clock = [0.0]

    def run() -> object:
        clock[0] += 0.001
        now = clock[0]
        return bucket.allow(now) and window.exceeded(now)

    return run


# ---------------------------------------------------------------- textmatch

SAMPLES = [
    "/dice@CookieMWbot 20",
    "/shippar @someone",
    "hello everyone, nothing to see here",
    "look https://bsky.app/profile/x/post/123 nice",
    "/d20",
]


def make_textmatch(parse: Callable[[str, str], object]) -> Callable[[], object]:
    i = [0]

    def run() -> object:
        i[0] = (i[0] + 1) % len(SAMPLES)
        return parse(SAMPLES[i[0]], "CookieMWbot")

    return run


# ------------------------------------------------------------------- dedupe


def make_dedupe(cls: object) -> Callable[[], object]:
    seen = cls(capacity=65536)
    i = [0]

    def run() -> object:
        i[0] += 1
        return seen.seen(i[0])

    return run


CASES: dict[str, tuple[int, dict[str, Callable[[], Callable[[], object]]]]] = {
    "cooldowns": (
        500_000,
        {
            "python": lambda: make_cooldowns(py_cooldowns),
            "cython": lambda: make_cooldowns(cy_cooldowns),
            "mojo": lambda: make_cooldowns(cb_hot),
        },
    ),
    "textmatch": (
        200_000,
        {
            "python": lambda: make_textmatch(py_textmatch.parse_command),
            "cython": lambda: make_textmatch(cy_textmatch.parse_command),
            "mojo": lambda: make_textmatch(cb_hot.CommandTable().parse_command),
        },
    ),
    "dedupe": (
        500_000,
        {
            "python": lambda: make_dedupe(py_dedupe.RecentIds),
            "cython": lambda: make_dedupe(cy_dedupe.RecentIds),
            "mojo": lambda: make_dedupe(cb_hot.RecentIds),
        },
    ),
}


def main() -> int:
    print(f"CPython {sys.version.split()[0]} — best of {ROUNDS} rounds\n")
    print(f"{'case':<12} {'variant':<8} {'ns/op':>9} {'vs python':>10} {'vs cython':>10}")
    for case, (iters, variants) in CASES.items():
        results = {name: bench(factory(), iters) for name, factory in variants.items()}
        base_py = results["python"]
        base_cy = results["cython"]
        for name, ns in results.items():
            print(
                f"{case:<12} {name:<8} {ns:>9.1f} "
                f"{base_py / ns:>9.2f}x {base_cy / ns:>9.2f}x"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
