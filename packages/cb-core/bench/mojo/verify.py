"""Parity check: the Mojo port must agree with the pure-Python cb_core modules."""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import cb_hot  # noqa: E402
from py import cooldowns as py_cooldowns  # noqa: E402
from py import dedupe as py_dedupe  # noqa: E402
from py import textmatch as py_textmatch  # noqa: E402

failures = 0


def check(label: str, got: object, want: object) -> None:
    global failures
    if got != want:
        failures += 1
        print(f"MISMATCH {label}: mojo={got!r} python={want!r}")


# --- TokenBucket
pb = py_cooldowns.TokenBucket(capacity=20.0, rate=5.0)
mb = cb_hot.TokenBucket(capacity=20.0, rate=5.0)
clock = 0.0
for i in range(200):
    clock += 0.001 if i % 3 else 0.5
    check(f"bucket.allow[{i}]", mb.allow(clock), pb.allow(clock))
check("bucket.tokens", round(mb.tokens_left(), 9), round(pb.tokens, 9))

# --- SlidingWindow
pw = py_cooldowns.SlidingWindow(limit=5, window=10.0)
mw = cb_hot.SlidingWindow(limit=5, window=10.0)
clock = 0.0
for i in range(500):
    clock += 0.3 if i % 7 else 4.0
    check(f"window.hit[{i}]", mw.hit(clock), pw.hit(clock))
check("window.count", mw.count(), pw.count)

# --- QuotaLedger
pq = py_cooldowns.QuotaLedger(limit=10)
mq = cb_hot.QuotaLedger(limit=10)
for i in range(300):
    key = i % 7
    day = i // 100
    check(f"quota.take[{i}]", mq.take(key, day), pq.take(key, day))
    check(f"quota.remaining[{i}]", mq.remaining(key, day), pq.remaining(key, day))

# --- RecentIds (LRU eviction order must match)
pr = py_dedupe.RecentIds(capacity=64)
mr = cb_hot.RecentIds(capacity=64)
for i in range(1000):
    uid = (i * 37) % 200
    check(f"recent.seen[{i}]", mr.seen(uid), pr.seen(uid))
check("recent.len", mr.size(), len(pr))

# --- parse_command
table = cb_hot.CommandTable()
SAMPLES = [
    "/dice@CookieMWbot 20",
    "/dice@OtherBot 20",
    "/shippar @someone",
    "hello everyone, nothing to see here",
    "/d20",
    "/d99999 extra args",
    "/d",
    "/dxx",
    "",
    "/",
    "/  spaced",
    "/CONFIGURAR",
    "/aleatório",
    "/ALEATÓRIO",
    "/Dice@cookiemwbot 7",
    "/RePoRt",
    "/cumpleaños  1990-01-02  ",
    "/unknowncmd",
    "/adm@CookieMWbot",
    "/report help me",
]
for text in SAMPLES:
    py_res = py_textmatch.parse_command(text, "CookieMWbot")
    mo_res = table.parse_command(text, "CookieMWbot")
    want = None if py_res is None else (py_res.name, py_res.args, py_res.target_bot)
    check(f"parse_command({text!r})", mo_res, want)

print("FAIL" if failures else "OK — Mojo port matches pure Python on every case")
raise SystemExit(1 if failures else 0)
