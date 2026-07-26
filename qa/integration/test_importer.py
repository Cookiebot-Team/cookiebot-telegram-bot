"""The v1 Mongo importer end to end, against a real Citus database.

Drives `cb_worker.importer.runner.run_import` with a hand-rolled, in-memory
`MongoSource` (this suite owns `FakeMongoSource` below — it is not `source.py`,
which reads a real live MongoDB or a `mongodump` directory; see
`importer/__init__.py`'s contract) carrying documents shaped like the real v1
collections (field names transcribed in `mappers.py`'s docstrings from the Java
`@Document` entities), through the *real* `mappers.MAPPERS` and the *real*
`loader.TABLE_LOADS`, then asserts the actual rows landed in `groups`,
`group_admins`, `group_configs`, `group_rules`, `group_welcomes`, `users` and
`blacklist`.

Three things this proves that a unit test cannot:
  * idempotency for real — a second `run_import` against the same (or an
    updated) source must not duplicate a row, and must not reset a column the
    import does not own (`loader.py`'s module docstring states exactly which
    columns those are).
  * the FK safety net — a `configs` document for a group `groups` never
    mentioned must still land, via `loader.ensure_group_stubs`, rather than
    aborting the whole collection or the whole run.
  * the real `TABLE_LOADS` column shapes actually lining up, positionally, with
    the real `mappers.MAPPERS` row tuples — only a real INSERT can prove that.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable, Coroutine, Iterator, Sequence
from types import ModuleType
from typing import Any

import pytest

from cb_core import db
from cb_core.group_config import DEFAULTS
from cb_worker.importer import Document
from cb_worker.importer.runner import run_import

pytestmark = pytest.mark.integration

Run = Callable[[Coroutine[Any, Any, Any]], Any]

# A range disjoint from qa/integration/factories.py's World
# (-1_00_000_000_000 - ...) and from qa/conftest.py's GROUP_ID, so this suite
# can never collide with either even if it runs concurrently with them.
_RUN_BASE = -9_00_000_000_000 - random.randrange(1, 9_000_000)
_USER_BASE = 800_000_000 + random.randrange(1, 900_000) * 1000
_group_seq = itertools.count(1)
_user_seq = itertools.count(1)


class FakeMongoSource:
    """Hand-rolled `MongoSource`: an in-memory dict of collection -> documents.

    Deliberately not `source.py` — the whole point of this suite is exercising
    `runner.py` / `loader.py` / `mappers.py` with no real MongoDB anywhere in
    the loop, per `importer/__init__.py`'s "three layers, deliberately
    separate."
    """

    def __init__(self, docs: dict[str, list[Document]]) -> None:
        self._docs = docs

    def collections(self) -> Sequence[str]:
        return list(self._docs)

    def read(self, collection: str) -> Iterator[Document]:
        yield from self._docs.get(collection, [])

    def count(self, collection: str) -> int | None:
        return len(self._docs.get(collection, []))

    def close(self) -> None:
        pass


class IdFactory:
    """Fresh, disjoint group/user ids for one test, tracked for teardown cleanup."""

    def __init__(self) -> None:
        self.group_ids: list[int] = []
        self.user_ids: list[int] = []

    def group(self) -> int:
        group_id = _RUN_BASE - next(_group_seq) * 10
        self.group_ids.append(group_id)
        return group_id

    def user(self) -> int:
        user_id = _USER_BASE + next(_user_seq)
        self.user_ids.append(user_id)
        return user_id


@pytest.fixture
def ids(run: Run) -> Iterator[IdFactory]:
    factory = IdFactory()
    yield factory
    # `groups` cascades to group_configs/group_rules/group_welcomes/group_admins
    # (ON DELETE CASCADE, migration 0001), so one delete cleans up four tables.
    if factory.group_ids:
        run(
            db.execute(
                "DELETE FROM groups WHERE group_id = ANY($1::bigint[])",
                factory.group_ids,
                name="test_importer_cleanup_groups",
            )
        )
    if factory.user_ids:
        run(
            db.execute(
                "DELETE FROM users WHERE user_id = ANY($1::bigint[])",
                factory.user_ids,
                name="test_importer_cleanup_users",
            )
        )
        run(
            db.execute(
                "DELETE FROM blacklist WHERE subject_id = ANY($1::bigint[])",
                factory.user_ids,
                name="test_importer_cleanup_blacklist",
            )
        )


# --------------------------------------------------------------------- documents


def _group_doc(group_id: int, admin_id: int, *, title: str = "Imported Group") -> Document:
    """Shaped like `Group.java`: `groupId`, `name`, `imageUrl`, `adminUsers`."""
    return {
        "groupId": str(group_id),
        "name": title,
        "imageUrl": "https://example.com/pic.png",
        "adminUsers": [str(admin_id)],
    }


def _configs_doc(group_id: int, **overrides: Any) -> Document:
    """Shaped like `Config.java`. Every field deliberately differs from v2's SQL
    default, so a passing assertion proves the mapped value round-tripped
    rather than merely matching what the column defaults to anyway."""
    base: dict[str, Any] = {
        "_id": str(group_id),
        "furbots": False,
        "stickerSpamLimit": "3",  # Config.java types this a String
        "timeWithoutSendingImages": 120,
        "timeCaptcha": 90,
        "functionsFun": False,
        "functionsUtility": True,
        "sfw": False,
        "language": "pt",
        "publisherPost": True,
        "publisherAsk": False,
        "publisherMembersOnly": True,
        "threadPosts": "9999",  # v1's "no forum topic" sentinel -> NULL
        "maxPosts": 42,
    }
    base.update(overrides)
    return base


def _rules_doc(group_id: int, text: str) -> Document:
    return {"_id": str(group_id), "rules": text}


def _welcome_doc(group_id: int, text: str) -> Document:
    return {"_id": str(group_id), "message": text}


def _user_doc(user_id: int, *, username: str = "importeduser") -> Document:
    return {
        "_id": str(user_id),
        "username": username,
        "firstName": "Imported",
        "lastName": "User",
        "languageCode": "es",
        "birthdate": None,
    }


def _blacklist_doc(subject_id: int) -> Document:
    """`Blacklist.java` is nothing but an `@Id` — mappers.py derives everything
    else from the id itself."""
    return {"_id": str(subject_id)}


# ------------------------------------------------------------------- clean import


class TestCleanImport:
    def test_every_table_gets_the_mapped_row(
        self, run: Run, pg: ModuleType, ids: IdFactory
    ) -> None:
        group_id = ids.group()
        admin_id = ids.user()
        user_id = ids.user()
        blacklisted_id = ids.user()

        source = FakeMongoSource(
            {
                "groups": [_group_doc(group_id, admin_id)],
                "configs": [_configs_doc(group_id)],
                "rules": [_rules_doc(group_id, "Be nice to each other.")],
                "welcomes": [_welcome_doc(group_id, "Welcome to the group!")],
                "users": [_user_doc(user_id)],
                "blacklist": [_blacklist_doc(blacklisted_id)],
            }
        )

        report = run(run_import(source, batch_size=500))

        assert report.read == {
            "groups": 1,
            "configs": 1,
            "rules": 1,
            "welcomes": 1,
            "users": 1,
            "blacklist": 1,
        }
        assert report.written == {
            "groups": 1,
            "group_admins": 1,
            "group_configs": 1,
            "group_rules": 1,
            "group_welcomes": 1,
            "users": 1,
            "blacklist": 1,
        }
        assert report.skipped == []

        group_row = run(
            pg.fetchrow(
                "SELECT title, image_url FROM groups WHERE group_id = $1",
                group_id,
                name="test_importer_read_group",
            )
        )
        assert group_row is not None
        assert group_row["title"] == "Imported Group"
        assert group_row["image_url"] == "https://example.com/pic.png"

        admin_row = run(
            pg.fetchrow(
                "SELECT role, anonymous FROM group_admins WHERE group_id = $1 AND user_id = $2",
                group_id,
                admin_id,
                name="test_importer_read_admin",
            )
        )
        assert admin_row is not None
        assert admin_row["role"] == "administrator"
        assert admin_row["anonymous"] is False

        config_row = run(
            pg.fetchrow(
                "SELECT * FROM group_configs WHERE group_id = $1",
                group_id,
                name="test_importer_read_config",
            )
        )
        assert config_row is not None
        assert config_row["allow_furbots"] is False
        assert config_row["sticker_spam_limit"] == 3
        assert config_row["media_restrict_seconds"] == 120
        assert config_row["captcha_timeout_seconds"] == 90
        assert config_row["functions_fun"] is False
        assert config_row["functions_utility"] is True
        assert config_row["sfw"] is False
        assert config_row["language"] == "pt"
        assert config_row["publisher_post"] is True
        assert config_row["publisher_ask"] is False
        assert config_row["publisher_members_only"] is True
        assert config_row["thread_posts"] is None
        assert config_row["max_posts"] == 42
        # No v1 field exists for either — the mapper writes v1's true default
        # (DEFAULTS), not a value the import should ever reassert; compared
        # against DEFAULTS itself, not a second hardcoded copy of the numbers
        # (same reasoning as qa/integration/test_group_config.py).
        assert config_row["sticker_spam_window_s"] == DEFAULTS.sticker_spam_window_s
        assert config_row["doomlist_enabled"] == DEFAULTS.doomlist_enabled

        rules_row = run(
            pg.fetchrow(
                "SELECT body FROM group_rules WHERE group_id = $1",
                group_id,
                name="test_importer_read_rules",
            )
        )
        assert rules_row is not None
        assert rules_row["body"] == "Be nice to each other."

        welcome_row = run(
            pg.fetchrow(
                "SELECT body FROM group_welcomes WHERE group_id = $1",
                group_id,
                name="test_importer_read_welcome",
            )
        )
        assert welcome_row is not None
        assert welcome_row["body"] == "Welcome to the group!"

        user_row = run(
            pg.fetchrow(
                "SELECT username, first_name, last_name, language_code "
                "FROM users WHERE user_id = $1",
                user_id,
                name="test_importer_read_user",
            )
        )
        assert user_row is not None
        assert user_row["username"] == "importeduser"
        assert user_row["first_name"] == "Imported"
        assert user_row["last_name"] == "User"
        assert user_row["language_code"] == "es"

        blacklist_row = run(
            pg.fetchrow(
                "SELECT kind, reason, source FROM blacklist WHERE subject_id = $1",
                blacklisted_id,
                name="test_importer_read_blacklist",
            )
        )
        assert blacklist_row is not None
        assert blacklist_row["kind"] == "user"  # positive id -> user, per mapper
        assert blacklist_row["reason"] is None
        assert blacklist_row["source"] == "manual"

    def test_a_document_with_an_unparseable_id_is_skipped_not_fatal(
        self, run: Run, ids: IdFactory
    ) -> None:
        source = FakeMongoSource({"rules": [{"_id": "not-a-number", "rules": "text"}]})

        report = run(run_import(source, batch_size=500))

        assert report.read == {"rules": 1}
        assert report.written == {}
        assert len(report.skipped) == 1
        assert report.skipped[0].collection == "rules"


# ------------------------------------------------------------------------ re-run


class TestReRun:
    def test_rerun_does_not_duplicate_any_row(
        self, run: Run, pg: ModuleType, ids: IdFactory
    ) -> None:
        group_id = ids.group()
        admin_id = ids.user()
        source = FakeMongoSource(
            {
                "groups": [_group_doc(group_id, admin_id)],
                "rules": [_rules_doc(group_id, "First version.")],
            }
        )

        report_first = run(run_import(source, batch_size=500))
        report_second = run(run_import(source, batch_size=500))

        assert report_first.written == report_second.written

        for table, expected in (
            ("groups", 1),
            ("group_admins", 1),
            ("group_rules", 1),
        ):
            row = run(
                pg.fetchrow(
                    f"SELECT count(*) AS n FROM {table} WHERE group_id = $1",
                    group_id,
                    name=f"test_importer_rerun_count_{table}",
                )
            )
            assert row is not None
            assert row["n"] == expected

    def test_rerun_catches_a_v1_delta_but_never_touches_a_v2_owned_column(
        self, run: Run, pg: ModuleType, ids: IdFactory
    ) -> None:
        """A re-run must reassert a real v1 change (rules text edited, a
        group's title renamed) -- that is the entire "catch the delta while v1
        still serves" point of the importer -- but must never reset a column
        v2 itself owns, like `groups.left_at`, which only the gateway sets when
        it observes the bot actually leaving a chat.
        """
        group_id = ids.group()
        admin_id = ids.user()

        run(
            run_import(
                FakeMongoSource(
                    {
                        "groups": [_group_doc(group_id, admin_id, title="Original Title")],
                        "rules": [_rules_doc(group_id, "First version.")],
                    }
                ),
                batch_size=500,
            )
        )

        # Only cb-gateway ever sets this, on a real "bot removed from chat"
        # update -- never the importer.
        run(
            db.execute(
                "UPDATE groups SET left_at = now() WHERE group_id = $1",
                group_id,
                name="test_importer_simulate_left",
            )
        )

        run(
            run_import(
                FakeMongoSource(
                    {
                        "groups": [_group_doc(group_id, admin_id, title="Updated Title")],
                        "rules": [_rules_doc(group_id, "Second version.")],
                    }
                ),
                batch_size=500,
            )
        )

        group_row = run(
            pg.fetchrow(
                "SELECT title, left_at FROM groups WHERE group_id = $1",
                group_id,
                name="test_importer_read_group_after_rerun",
            )
        )
        assert group_row is not None
        assert group_row["title"] == "Updated Title"  # v1-owned: the delta landed
        assert group_row["left_at"] is not None  # v2-owned: not resurrected

        rules_row = run(
            pg.fetchrow(
                "SELECT body FROM group_rules WHERE group_id = $1",
                group_id,
                name="test_importer_read_rules_after_rerun",
            )
        )
        assert rules_row is not None
        assert rules_row["body"] == "Second version."

    def test_rerun_never_resets_the_two_v2_only_config_columns(
        self, run: Run, pg: ModuleType, ids: IdFactory
    ) -> None:
        group_id = ids.group()
        source = FakeMongoSource({"configs": [_configs_doc(group_id)]})
        run(run_import(source, batch_size=500))

        # A hypothetical v2-only feature (or an admin, once one exists) changes
        # a column the import has no v1 signal for at all.
        run(
            db.execute(
                "UPDATE group_configs SET sticker_spam_window_s = 120, doomlist_enabled = false "
                "WHERE group_id = $1",
                group_id,
                name="test_importer_simulate_v2_edit",
            )
        )

        run(run_import(source, batch_size=500))

        row = run(
            pg.fetchrow(
                "SELECT sticker_spam_window_s, doomlist_enabled FROM group_configs "
                "WHERE group_id = $1",
                group_id,
                name="test_importer_read_config_after_rerun",
            )
        )
        assert row is not None
        assert row["sticker_spam_window_s"] == 120
        assert row["doomlist_enabled"] is False


# -------------------------------------------------------------- missing groups doc


class TestMissingGroupDocument:
    def test_configs_without_a_groups_document_still_lands_via_a_stub_group(
        self, run: Run, pg: ModuleType, ids: IdFactory
    ) -> None:
        group_id = ids.group()  # deliberately never appears in a "groups" document

        source = FakeMongoSource({"configs": [_configs_doc(group_id, maxPosts=7)]})

        report = run(run_import(source, batch_size=500))

        assert report.skipped == []
        assert report.written.get("group_configs") == 1

        stub_row = run(
            pg.fetchrow(
                "SELECT title, image_url FROM groups WHERE group_id = $1",
                group_id,
                name="test_importer_read_stub_group",
            )
        )
        assert stub_row is not None  # loader.ensure_group_stubs created it
        assert stub_row["title"] is None
        assert stub_row["image_url"] is None

        config_row = run(
            pg.fetchrow(
                "SELECT max_posts FROM group_configs WHERE group_id = $1",
                group_id,
                name="test_importer_read_stubbed_config",
            )
        )
        assert config_row is not None
        assert config_row["max_posts"] == 7

    def test_a_real_groups_document_arriving_later_is_not_blocked_by_the_stub(
        self, run: Run, pg: ModuleType, ids: IdFactory
    ) -> None:
        """`ensure_group_stubs` is `ON CONFLICT DO NOTHING` -- if the real
        `groups` document turns up in a later run (Mongo's collections are read
        independently; there is no guarantee `groups` is ever complete when
        `configs` is), it must still land normally: the earlier stub never
        claimed `title`/`image_url` for itself.
        """
        group_id = ids.group()
        admin_id = ids.user()

        run(run_import(FakeMongoSource({"configs": [_configs_doc(group_id)]}), batch_size=500))
        run(
            run_import(
                FakeMongoSource({"groups": [_group_doc(group_id, admin_id, title="Real Title")]}),
                batch_size=500,
            )
        )

        row = run(
            pg.fetchrow(
                "SELECT title FROM groups WHERE group_id = $1",
                group_id,
                name="test_importer_read_group_after_stub",
            )
        )
        assert row is not None
        assert row["title"] == "Real Title"


# --------------------------------------------------------------------------- misc


class TestDryRun:
    def test_dry_run_writes_nothing_but_still_reports_counts(
        self, run: Run, pg: ModuleType, ids: IdFactory
    ) -> None:
        group_id = ids.group()
        source = FakeMongoSource({"configs": [_configs_doc(group_id)]})

        report = run(run_import(source, dry_run=True, batch_size=500))

        assert report.written == {"group_configs": 1}

        row = run(
            pg.fetchrow(
                "SELECT 1 FROM group_configs WHERE group_id = $1",
                group_id,
                name="test_importer_dry_run_no_row",
            )
        )
        assert row is None
        group_row = run(
            pg.fetchrow(
                "SELECT 1 FROM groups WHERE group_id = $1",
                group_id,
                name="test_importer_dry_run_no_stub",
            )
        )
        assert group_row is None  # not even the FK stub -- dry run touches no table
