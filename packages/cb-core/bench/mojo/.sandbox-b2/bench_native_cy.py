"""Cython counterpart of bench_native.mojo — the loop itself is compiled."""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import cynative  # noqa: E402

assert cynative.COMPILED, "cynative must be compiled"

raise SystemExit(cynative.main())
