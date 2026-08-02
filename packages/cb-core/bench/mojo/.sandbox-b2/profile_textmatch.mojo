"""Where do textmatch's ~800 ns/op go in the Mojo build?"""

from std.time import perf_counter_ns
from std.collections import Dict
from cb_hot import CommandTable

comptime N = 200_000


def keep(value: Int):
    if value == -12345:
        print("unreachable", value)


def main() raises:
    var samples = [
        String("/dice@CookieMWbot 20"),
        String("/shippar @someone"),
        String("hello everyone, nothing to see here"),
        String("look https://bsky.app/profile/x/post/123 nice"),
        String("/d20"),
    ]
    var keys = [String("dice"), String("shippar"), String("hello"), String("look"), String("d20")]
    var table = CommandTable()

    # 1. dict lookup only
    var sink = 0
    var start = perf_counter_ns()
    for i in range(N):
        if table.aliases.get(keys[i % 5], String("")):
            sink += 1
    print("dict.get           ", Float64(perf_counter_ns() - start) / N, "ns/op")
    keep(sink)

    # 2. lower() only
    sink = 0
    start = perf_counter_ns()
    for i in range(N):
        sink += len(keys[i % 5].lower())
    print("String.lower       ", Float64(perf_counter_ns() - start) / N, "ns/op")
    keep(sink)

    # 3. one String allocation from a byte slice
    sink = 0
    start = perf_counter_ns()
    for i in range(N):
        sink += len(String(samples[i % 5][byte=1:4]))
    print("String(slice)      ", Float64(perf_counter_ns() - start) / N, "ns/op")
    keep(sink)

    # 4. strip()
    sink = 0
    start = perf_counter_ns()
    for i in range(N):
        sink += len(samples[i % 5].strip())
    print("String.strip       ", Float64(perf_counter_ns() - start) / N, "ns/op")
    keep(sink)

    # 5. find("@")
    sink = 0
    start = perf_counter_ns()
    for i in range(N):
        sink += samples[i % 5].find("@")
    print("String.find        ", Float64(perf_counter_ns() - start) / N, "ns/op")
    keep(sink)

    # 6. full parse for reference
    sink = 0
    start = perf_counter_ns()
    for i in range(N):
        if table.parse_impl(samples[i % 5], String("CookieMWbot")):
            sink += 1
    print("parse_impl (full)  ", Float64(perf_counter_ns() - start) / N, "ns/op")
    keep(sink)
