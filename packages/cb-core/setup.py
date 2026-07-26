"""Cython build for the hot path only.

Policy (see docs/ARCHITECTURE.md §2): compiling `await`-heavy IO code buys nothing.
Only pure-CPU, no-IO modules are compiled, and they stay valid Python — the same
.py files import and run uncompiled, so tests and debugging work either way.

`python scripts/cb.py bench` gates this: a module that does not reach 1.5x compiled ships pure.
"""

from __future__ import annotations

import os

from setuptools import setup

HOT_MODULES = [
    "src/cb_core/cooldowns.py",  # token bucket, runs on every single update
    # Three modules were dropped from this list by the benchmark gate, which is
    # exactly what the gate is for:
    #
    # cb_core/captcha.py   1.00x — bounded by a CSPRNG syscall in secrets.randbelow.
    # cb_core/textmatch.py 1.48-1.55x over eight runs, straddling the 1.5x line.
    #   Its cost is Python string and dict work that Cython cannot lower much. A
    #   marginal win is not worth a CI gate that fails one run in four.
    # cb_core/dedupe.py    1.40-1.47x over four runs against a freshly recorded
    #   baseline — below the gate every time. It had been recorded at 1.60x, but
    #   that number came from a baseline `cb.py bench-baseline` never actually
    #   wrote: the in-place .so survived the pure reinstall, so the "uncompiled"
    #   run was the compiled one and bench_hot.py skipped writing the file
    #   (it only writes when nothing is compiled). The task now deletes the
    #   extensions first, and the honest number does not clear the bar. Its work
    #   is a set lookup and a blake3 call — both already C.
]

ext_modules = []
if os.environ.get("CB_SKIP_CYTHON") != "1":
    try:
        from Cython.Build import cythonize

        ext_modules = cythonize(
            HOT_MODULES,
            language_level="3",
            annotate=True,  # emit .html so we can see remaining Python interaction
            compiler_directives={
                "annotation_typing": True,  # honour PEP 484 hints as C types
                "boundscheck": False,
                "wraparound": False,
                "cdivision": True,
                "initializedcheck": False,
                "binding": True,
                "embedsignature": True,
                "profile": False,
            },
        )
    except ImportError:  # pragma: no cover - pure-python fallback
        ext_modules = []

setup(ext_modules=ext_modules)
