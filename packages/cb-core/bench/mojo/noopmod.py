"""Call-overhead reference: the same one-float-in, one-bool-out signature."""
from __future__ import annotations

import cython

COMPILED: bool = cython.compiled


@cython.cclass
class Noop:
    @cython.ccall
    def noop(self, value: cython.double) -> cython.bint:
        return value > 0.0
