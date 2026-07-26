"""Startup schema convergence — the parts that do not need a database.

The upgrade path itself is covered by the `migrations` CI job; what is tested
here is everything that decides *whether* to upgrade, because that code runs in
every process on every boot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cb_core import migrations
from cb_core.migrations import _sqlalchemy_url
from cb_core.settings import Settings


def test_finds_the_revisions_without_configuration() -> None:
    """Discovery must work from an editable workspace install with no env var set."""
    directory = migrations.migrations_dir(Settings())
    assert (directory / "versions").is_dir()
    assert directory.name == "migrations"


def test_head_is_single_and_matches_the_newest_revision() -> None:
    directory = migrations.migrations_dir(Settings())
    head = migrations.head_revision(directory)
    files = sorted(p.name for p in (directory / "versions").glob("[0-9]*.py"))
    assert head == files[-1].split("_", 1)[0]


def test_explicit_directory_is_validated(tmp_path: Path) -> None:
    with pytest.raises(migrations.MigrationError, match="versions"):
        migrations.migrations_dir(Settings(migrations_dir=str(tmp_path)))


async def test_disabled_never_touches_the_database() -> None:
    """CB_AUTO_MIGRATE=false must not even resolve a DSN — the pg_dsn here is bogus."""
    settings = Settings(auto_migrate=False, pg_dsn="postgresql://nobody@127.0.0.1:1/none")
    assert await migrations.ensure_schema(settings) == "disabled"


def test_dsn_is_rewritten_for_sqlalchemy() -> None:
    assert _sqlalchemy_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    # Already-qualified DSNs pass through untouched.
    assert _sqlalchemy_url("postgresql+psycopg://h/db") == "postgresql+psycopg://h/db"
