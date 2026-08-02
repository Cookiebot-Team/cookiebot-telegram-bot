#!/usr/bin/env bash
# Every benchmark, in the order the architecture doc reads them.
set -euo pipefail

cd "$(dirname "$0")/.sandbox"
[ -f cb_hot.so ] || { echo "run ../setup_env.sh first" >&2; exit 1; }

./.venv/bin/python verify.py
echo
./.venv/bin/python bench_all.py
./.venv/bin/python bench_call.py
echo
./.venv/bin/python bench_batch2.py
echo
./.venv/bin/mojo run -O3 bench_native.mojo
echo
./.venv/bin/mojo run -O3 profile_textmatch.mojo
