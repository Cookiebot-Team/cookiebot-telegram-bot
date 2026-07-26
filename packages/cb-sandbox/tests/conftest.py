"""Pin these tests to the sandbox's own built-in defaults.

`cb_sandbox.config` discovers `sandbox.config.json` from the working directory
upwards, which is exactly what a bot repository wants and exactly wrong here:
run from a repository root that ships one, this package's tests would assert
against *that bot's* identity and seeds rather than the defaults they are
testing. Forcing `DEFAULT_CONFIG` makes the suite say the same thing whether
it runs standalone or inside a host application.

Tests that need a *different* config call `set_config` themselves; this
fixture restores the default afterwards either way.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cb_sandbox.config import DEFAULT_CONFIG, reset_config, set_config


@pytest.fixture(autouse=True)
def _built_in_config() -> Iterator[None]:
    set_config(DEFAULT_CONFIG)
    yield
    reset_config()
