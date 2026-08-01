"""The sandbox test kit, re-exported under the name this suite already uses.

Everything here now lives in `tg_sandbox.testkit` — the sandbox package ships
its own client and pytest plugin so any bot repository gets them by installing
it, rather than each one growing a hand-written copy that drifts from the
control API it talks to.

This module stays as a one-line import site because every step file in this
package imports from it, and a re-export is a smaller change than touching all
of them to say the same thing.
"""

from __future__ import annotations

from tg_sandbox.testkit import (
    SandboxClient,
    calls_to,
    describe_recent_calls,
    messages_in,
    wait_for,
)

__all__ = [
    "SandboxClient",
    "calls_to",
    "describe_recent_calls",
    "messages_in",
    "wait_for",
]
