"""The test kit: drive the sandbox from a test suite the same way a human
drives it from the browser.

Two pieces, and the split matters:

`client.py`  a thin, synchronous HTTP client for the `/api/...` control plane
             plus the assertion helpers a bot test actually needs — "what did
             the bot ask Telegram to do", and a bounded poll for it. No
             pytest, no fixtures: importable from a plain script, a Django
             management command, or a different test runner entirely.

`plugin.py`  a pytest plugin, registered as an entry point, so installing this
             package gives any suite a `sandbox` fixture, a running sandbox
             server, and one scenario per test — tagged with the feature it
             was checking and closed with that test's real outcome.

The scenario tagging is the part worth understanding. Every message and every
Bot API call recorded while a scenario is active carries its id, so after a
run the DuckDB file the sandbox leaves behind is not an undifferentiated
stream: it is filterable down to one test, and groupable up to one feature.
Pointing a sandbox server at that file and opening the web client shows what
the suite actually did, which is a strictly better answer to "does the bot
work" than a row of green dots.
"""

from __future__ import annotations

from cb_sandbox.testkit.client import (
    SandboxClient,
    calls_to,
    describe_recent_calls,
    messages_in,
    wait_for,
)
from cb_sandbox.testkit.process import SandboxProcess

__all__ = [
    "SandboxClient",
    "SandboxProcess",
    "calls_to",
    "describe_recent_calls",
    "messages_in",
    "wait_for",
]
