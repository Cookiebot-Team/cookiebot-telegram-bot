"""Cython gate: a hot module must reach >=1.5x compiled or it ships pure Python.

    python scripts/cb.py cython && python scripts/cb.py bench

Prints per-module ns/op and, when a baseline from the uncompiled run exists,
the speedup. CI runs it twice (CB_SKIP_CYTHON=1 first) and compares.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from collections.abc import Callable

from cb_core import captcha, cooldowns, dedupe, textmatch

MIN_SPEEDUP = 1.5
BASELINE = pathlib.Path(os.environ.get("CB_BENCH_BASELINE", "/tmp/cb-bench-baseline.json"))


def bench(fn: Callable[[], object], iterations: int = 200_000, rounds: int = 5) -> float:
    """Best-of-N mean ns/op — best-of resists noisy CI runners."""
    timings: list[float] = []
    for _ in range(rounds):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            fn()
        timings.append((time.perf_counter_ns() - start) / iterations)
    return min(timings)


def _cooldowns() -> Callable[[], object]:
    bucket = cooldowns.TokenBucket(capacity=20.0, rate=5.0)
    window = cooldowns.SlidingWindow(limit=5, window=10.0)
    clock = [0.0]

    def run() -> object:
        clock[0] += 0.001
        now = clock[0]
        return bucket.allow(now) and window.exceeded(now)

    return run


def _textmatch() -> Callable[[], object]:
    samples = [
        "/dice@CookieMWbot 20",
        "/shippar @someone",
        "hello everyone, nothing to see here",
        "look https://bsky.app/profile/x/post/123 nice",
        "/d20",
    ]
    i = [0]

    def run() -> object:
        i[0] = (i[0] + 1) % len(samples)
        text = samples[i[0]]
        return textmatch.parse_command(text, "CookieMWbot") or textmatch.find_embeddable_links(text)

    return run


def _dedupe() -> Callable[[], object]:
    seen = dedupe.RecentIds(capacity=65536)
    i = [0]

    def run() -> object:
        i[0] += 1
        return seen.seen(i[0])

    return run


def _captcha() -> Callable[[], object]:
    def run() -> object:
        ch = captcha.make_arithmetic()
        return captcha.verify(ch.answer, ch.options[0])

    return run


CASES = {
    "cooldowns": (_cooldowns, 500_000),
    "textmatch": (_textmatch, 200_000),
    "dedupe": (_dedupe, 500_000),
    "captcha": (_captcha, 50_000),
}


def main() -> int:
    compiled = {
        "cooldowns": cooldowns.COMPILED,
        "textmatch": textmatch.COMPILED,
        "dedupe": dedupe.COMPILED,
        "captcha": captcha.COMPILED,
    }
    results: dict[str, float] = {}
    for name, (factory, iters) in CASES.items():
        results[name] = bench(factory(), iterations=iters)

    any_compiled = any(compiled.values())
    baseline: dict[str, float] = {}
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text())

    print(f"{'module':<12} {'compiled':<9} {'ns/op':>10} {'speedup':>9}  verdict")
    failures = 0
    for name, ns in results.items():
        base = baseline.get(name)
        speedup = (base / ns) if base else float("nan")
        verdict = "-"
        if compiled[name] and base:
            if speedup >= MIN_SPEEDUP:
                verdict = "keep compiled"
            else:
                verdict = f"SHIP PURE (<{MIN_SPEEDUP}x)"
                failures += 1
        print(f"{name:<12} {str(compiled[name]).lower():<9} {ns:>10.1f} {speedup:>9.2f}  {verdict}")

    if not any_compiled:
        BASELINE.write_text(json.dumps(results))
        print(f"\nbaseline written to {BASELINE} (uncompiled run)")
        return 0

    if not baseline:
        print(
            f"\nno baseline at {BASELINE} — cannot judge. Rebuild uncompiled first:\n"
            "  CB_SKIP_CYTHON=1 uv sync --reinstall-package cb-core && python scripts/cb.py bench"
        )
        return 1

    if failures:
        print(f"\n{failures} module(s) below the {MIN_SPEEDUP}x gate")
        return 1
    print("\nall compiled modules clear the gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
