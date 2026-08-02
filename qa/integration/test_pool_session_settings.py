"""The session settings every pooled connection is opened with.

These exist for a measured failure, not a hunch. asyncpg introspects a type the
first time a connection returns one it has not cached — `tenants.owner_ids`, a
`text[]`, is the first for most connections — and on a Citus database that
introspection query took 23.5 seconds, which `command_timeout` then cancelled.
The visible symptom was a `/config` write that did ~30ms of real work and
answered ten seconds later, having silently fallen back to the default tenant
because the lookup that triggered the introspection was the one that got
cancelled.

Measured on the cluster, same query, same connection:

    baseline                                23_500 ms
    + citus.override_table_visibility=off    4_308 ms
    + jit=off                                   19 ms

So this file asserts the settings are actually applied to a real connection —
a `SET` that silently failed would put the 23 seconds straight back, and
nothing else in the suite would notice, because everything else is fast either
way.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import Any

import pytest

from cb_core import db

pytestmark = pytest.mark.integration


class TestSessionSettingsAreApplied:
    def test_jit_is_off_on_a_pooled_connection(
        self, pg: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> None:
        """OLTP single-row lookups have nothing to win from JIT, and the plan
        for asyncpg's introspection query is large enough to cross
        `jit_above_cost` — four seconds of compiling to return five rows."""
        assert run(db.fetchrow("SHOW jit", name="test"))[0] == "off"

    def test_citus_shard_visibility_override_is_off(
        self, pg: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> None:
        """Citus hides shards by injecting `relation_is_a_known_shard(oid)` into
        every `pg_class` scan. The introspection query scans `pg_class` 485
        times, so that call ran 1.37 million times — nineteen of the twenty-three
        seconds."""
        assert run(db.fetchrow("SHOW citus.override_table_visibility", name="test"))[0] == "off"

    def test_the_type_introspection_they_guard_is_fast(
        self, pg: ModuleType, run: Callable[[Coroutine[Any, Any, Any]], Any]
    ) -> None:
        """The end the settings exist for: a query returning an array type has
        to introspect it, and that must not take seconds.

        Deliberately generous — this asserts "not pathological", not a
        benchmark. The failure it catches was 23_500ms; anything under a second
        means the catalog scan is not being filtered per row.
        """
        started = time.perf_counter()
        row = run(
            db.fetchrow(
                "SELECT tenant_id, owner_ids, disabled_commands, feature_defaults "
                "FROM tenants LIMIT 1",
                name="test",
            )
        )
        elapsed = time.perf_counter() - started
        assert row is not None, "no tenants row to introspect against"
        assert elapsed < 1.0, f"type introspection took {elapsed:.1f}s — see cb_core/db.py"
