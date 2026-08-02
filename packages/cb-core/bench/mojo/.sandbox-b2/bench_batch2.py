"""Which part of a batched Mojo call costs what."""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import cb_hot

BATCH, BATCHES, ROUNDS = 100, 5_000, 7
PREBUILT = list(range(BATCH))


def bench(fn) -> float:
    best = float("inf")
    for _ in range(ROUNDS):
        start = time.perf_counter_ns()
        fn()
        best = min(best, (time.perf_counter_ns() - start) / (BATCHES * BATCH))
    return best


def batch_list_out() -> None:
    seen = cb_hot.RecentIds(capacity=65536)
    for _ in range(BATCHES):
        seen.seen_many(PREBUILT)


def batch_count_out() -> None:
    seen = cb_hot.RecentIds(capacity=65536)
    for _ in range(BATCHES):
        seen.seen_many_count(PREBUILT)


def batch_no_collection() -> None:
    seen = cb_hot.RecentIds(capacity=65536)
    for i in range(BATCHES):
        seen.seen_range(i * BATCH, BATCH)


print(f"{'variant':<34} {'ns/update':>10}")
for label, fn in {
    "prebuilt list in, list of bools out": batch_list_out,
    "prebuilt list in, single int out": batch_count_out,
    "no Python collection at all": batch_no_collection,
}.items():
    print(f"{label:<34} {bench(fn):>10.1f}")
