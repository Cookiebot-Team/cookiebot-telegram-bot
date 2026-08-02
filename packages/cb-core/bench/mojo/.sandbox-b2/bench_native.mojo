"""Same hot-path workloads, driven from Mojo — no CPython boundary in the loop.

Pairs with bench_all.py: the difference between these numbers and the `mojo`
rows there is the cost of the Python <-> Mojo call, not of the algorithm.
"""

from std.time import perf_counter_ns
from cb_hot import CommandTable, RecentIds, SlidingWindow, TokenBucket

comptime ROUNDS = 7


def bench_cooldowns(iterations: Int) raises -> Float64:
    var best = Float64.MAX
    for _ in range(ROUNDS):
        var bucket = TokenBucket(20.0, 5.0)
        var window = SlidingWindow(5, 10.0)
        var clock: Float64 = 0.0
        var sink = 0
        var start = perf_counter_ns()
        for _ in range(iterations):
            clock += 0.001
            if bucket.allow_impl(clock) and window.exceeded_impl(clock):
                sink += 1
        var ns = Float64(perf_counter_ns() - start) / Float64(iterations)
        keep(sink)
        if ns < best:
            best = ns
    return best


def bench_dedupe(iterations: Int) raises -> Float64:
    var best = Float64.MAX
    for _ in range(ROUNDS):
        var seen = RecentIds(65536)
        var sink = 0
        var start = perf_counter_ns()
        for i in range(iterations):
            if seen.seen_impl(Int64(i)):
                sink += 1
        var ns = Float64(perf_counter_ns() - start) / Float64(iterations)
        keep(sink)
        if ns < best:
            best = ns
    return best


def bench_textmatch(iterations: Int) raises -> Float64:
    var samples = [
        String("/dice@CookieMWbot 20"),
        String("/shippar @someone"),
        String("hello everyone, nothing to see here"),
        String("look https://bsky.app/profile/x/post/123 nice"),
        String("/d20"),
    ]
    var bot = String("CookieMWbot")
    var best = Float64.MAX
    for _ in range(ROUNDS):
        var table = CommandTable()
        var sink = 0
        var start = perf_counter_ns()
        for i in range(iterations):
            var parsed = table.parse_impl(samples[i % 5], bot)
            if parsed:
                sink += 1
        var ns = Float64(perf_counter_ns() - start) / Float64(iterations)
        keep(sink)
        if ns < best:
            best = ns
    return best


def keep(value: Int):
    """Keep `value` observable so the optimiser cannot delete the loop body."""
    if value == -12345:
        print("unreachable", value)


def main() raises:
    print("mojo-native (no CPython boundary) — best of", ROUNDS, "rounds\n")
    print("cooldowns ", bench_cooldowns(500_000), "ns/op")
    print("textmatch ", bench_textmatch(200_000), "ns/op")
    print("dedupe    ", bench_dedupe(500_000), "ns/op")
