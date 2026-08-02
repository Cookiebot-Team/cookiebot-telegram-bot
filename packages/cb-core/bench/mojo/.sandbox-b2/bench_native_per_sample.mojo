"""Per-sample native parse cost — the Mojo half of the Cython/Mojo parser split.

bench_native.mojo averages five message shapes. The two languages' parsers do not
disagree uniformly across them (the Cython build hits a regex on `/d20`, the Mojo
build a hand-rolled digit loop), so the average hides where the gap actually is.
"""

from std.time import perf_counter_ns
from cb_hot import CommandTable

comptime ROUNDS = 7
comptime N = 200_000


def keep(value: Int):
    if value == -12345:
        print("unreachable", value)


def bench_one(table: CommandTable, sample: String, bot: String) raises -> Float64:
    var best = Float64.MAX
    for _ in range(ROUNDS):
        var sink = 0
        var start = perf_counter_ns()
        for _ in range(N):
            if table.parse_impl(sample, bot):
                sink += 1
        var ns = Float64(perf_counter_ns() - start) / Float64(N)
        keep(sink)
        if ns < best:
            best = ns
    return best


def main() raises:
    var samples = [
        String("/dice@CookieMWbot 20"),
        String("/shippar @someone"),
        String("hello everyone, nothing to see here"),
        String("look https://bsky.app/profile/x/post/123 nice"),
        String("/d20"),
    ]
    var bot = String("CookieMWbot")
    var table = CommandTable()
    print("mojo-native per sample — best of", ROUNDS, "rounds\n")
    for sample in samples:
        print("  sample", repr(sample[byte=0:24]), bench_one(table, sample, bot), "ns/op")
