"""GroupConfig against a real Citus database.

Exercises the read/write round trip `cb_core.group_config` replaces v1's
unbounded, un-invalidated `cache_configurations` dict with (FEATURE-MAP D6,
`docs/contracts/group-config.md`), plus the Citus single-shard guarantee the read
query depends on: `groups` LEFT JOIN `group_configs`, both colocated distributed
tables filtered on `group_id` (AGENTS.md §4).

No Valkey is required: `cb_core.cache` is not initialised in this suite, so every
L2 call fails closed and is treated as a cache miss, which the group_config layer
is designed to tolerate. What is exercised here is the Postgres round trip and the
tenant-merge path against the real `tenants`/`groups` seed data from migrations
0001/0003.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Coroutine, Iterator
from types import ModuleType
from typing import Any

import pytest

from cb_core import group_config
from cb_core.group_config import DEFAULTS
from qa.integration.factories import World

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]


@pytest.fixture(autouse=True)
def _clean_l1() -> Iterator[None]:
    group_config._l1.clear()  # noqa: SLF001
    yield
    group_config._l1.clear()  # noqa: SLF001


@pytest.fixture(scope="module")
def citus(pg: ModuleType, run: Run) -> ModuleType:
    """Same guard as qa/integration/test_citus_topology.py: skip on plain Postgres."""
    row = run(pg.fetchrow("SELECT count(*) AS n FROM pg_extension WHERE extname = 'citus'"))
    if not row or row["n"] == 0:
        pytest.skip("citus extension not installed")
    return pg


class TestRoundTrip:
    def test_reads_the_row_the_factory_seeded(self, run: Run, world: World) -> None:
        """`world.setup()` inserts a group_configs row with the SQL column defaults.

        Asserted against `DEFAULTS` rather than a second copy of the numbers: the
        SQL column defaults and the in-code defaults are two transcriptions of the
        same v1 tuple (`Configurations.py:111`), and they drifted apart once
        already — a group created on v2 was getting media restriction 0s instead
        of 600s and a 120s captcha instead of 300s. Comparing them here is what
        catches that, and a literal table would only re-record the drift.
        """
        config = run(group_config.get_config(world.group_id))

        assert config == dataclasses.replace(group_config.DEFAULTS, group_id=world.group_id)

    def test_defaults_for_a_group_with_no_config_row(
        self, run: Run, world: World, pg: ModuleType
    ) -> None:
        """A group can exist without ever having run /config — no row, DEFAULTS serve it."""
        run(
            pg.execute(
                "DELETE FROM group_configs WHERE group_id = $1",
                world.group_id,
                name="test_drop_config_row",
            )
        )

        config = run(group_config.get_config(world.group_id))

        # world's group has tenant_id='cookiebot' (groups.tenant_id default), and
        # the seeded 'cookiebot' tenant carries no feature_defaults overrides, so
        # this is DEFAULTS verbatim, just with the real group_id filled in.
        expected = dataclasses.replace(DEFAULTS, group_id=world.group_id)
        assert config == expected


class TestUpsert:
    def test_set_config_writes_and_is_immediately_visible(self, run: Run, world: World) -> None:
        updated = run(
            group_config.set_config(
                world.group_id, functions_fun=False, sticker_spam_limit=10, language="pt"
            )
        )

        assert updated.functions_fun is False
        assert updated.sticker_spam_limit == 10
        assert updated.language == "pt"

        # Round-trip through a clean L1 (invalidate() already dropped it, but a
        # second explicit get_config call proves the row itself changed, not just
        # the in-process cache set_config seeded).
        group_config._l1.clear()  # noqa: SLF001
        reread = run(group_config.get_config(world.group_id))
        assert reread == updated

    def test_set_config_only_touches_the_given_columns(self, run: Run, world: World) -> None:
        run(group_config.set_config(world.group_id, max_posts=42))
        run(group_config.set_config(world.group_id, sfw=False))

        config = run(group_config.get_config(world.group_id))

        assert config.max_posts == 42
        assert config.sfw is False
        # untouched columns keep their SQL defaults
        assert config.functions_fun is True

    def test_set_config_rejects_an_unknown_column(self, run: Run, world: World) -> None:
        with pytest.raises(ValueError, match="unknown"):
            run(group_config.set_config(world.group_id, not_a_real_column=True))

    def test_invalidate_drops_the_l1_entry(self, run: Run, world: World) -> None:
        run(group_config.get_config(world.group_id))
        assert group_config.cached_size() >= 1

        run(group_config.invalidate(world.group_id))

        assert world.group_id not in group_config._l1  # noqa: SLF001


class TestCitusTopology:
    """The read query must touch exactly one shard — AGENTS.md §4 rule 6."""

    def test_group_config_lookup_is_single_shard(
        self, citus: ModuleType, run: Run, world: World
    ) -> None:
        plan = run(
            citus.fetch(
                f"EXPLAIN (COSTS OFF) {group_config._SELECT}",  # noqa: SLF001
                world.group_id,
            )
        )
        task_count = None
        for row in plan:
            line = str(row[0]).strip()
            if line.startswith("Task Count:"):
                task_count = int(line.split(":")[1])
                break
        assert task_count is not None, "no Task Count in plan:\n" + "\n".join(
            str(r[0]) for r in plan
        )
        assert task_count == 1
