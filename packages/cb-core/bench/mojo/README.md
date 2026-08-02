# Mojo vs Cython on the hot path — the experiment behind the numbers

Not a build target. Nothing here is imported by `cb_core`, compiled by
`setup.py`, or run by CI. It exists so the claim in
`docs/site/content/docs/architecture.mdx` §2 ("keep Cython") can be re-checked
instead of believed.

## What it measures

`cb_hot.mojo` is a Mojo port of the three pure-CPU modules the Cython gate
cares about — `cooldowns.TokenBucket` / `SlidingWindow` / `QuotaLedger`,
`dedupe.RecentIds`, and `textmatch.parse_command` with its alias table. Same
algorithms, same observable behaviour; `verify.py` is the proof, and it fails
loudly on any drift.

Four benchmarks, all best-of-7 mean ns/op, all variants in one process:

| script | question |
|---|---|
| `bench_all.py` | pure Python vs Cython vs Mojo, called the way handlers call them |
| `bench_call.py` | what does one call *cost*, with no work inside it |
| `bench_native.mojo` | the same workloads with no CPython boundary at all |
| `bench_batch2.py` | where the Mojo cost sits when a whole batch crosses at once |
| `profile_textmatch.mojo` | which stdlib call dominates the parser (answer: `String.lower`) |

## Running it

```bash
./setup_env.sh          # builds .sandbox/ — venv, Mojo toolchain, both builds
./run.sh                # every benchmark, in order
```

`setup_env.sh` pins **CPython 3.12** rather than the workspace's 3.14. Not a
Mojo limitation — the built `.so` imports fine under 3.14 — it just keeps the
Cython and Mojo builds on one interpreter without touching the real `.venv`.
Everything lands in `.sandbox/`, which is gitignored; delete it to start over.

The Mojo toolchain (~2 GB) installs into that sandbox via `uv` from Modular's
index, so this costs one download and never touches the workspace lockfile.

## Reading the result

The short version: Mojo's compute is 5–13× faster than Cython's and its call
overhead is ~60 ns worse, so on code that does ~15 ns of work per call the
boundary decides the winner and Cython takes it. Full numbers and the verdict
are in the architecture doc.

If you re-run this after a Mojo release, the number to watch is
`bench_call.py`'s "mojo binding" row. Everything else follows from it.
