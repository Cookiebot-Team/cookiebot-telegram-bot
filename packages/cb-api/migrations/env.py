"""Alembic env — sync psycopg for DDL, asyncpg stays for runtime.

Citus DDL (create_distributed_table, create_reference_table) is written as raw SQL
in the revisions rather than reflected from models: shard keys and colocation are
decisions we want visible in the diff, not inferred.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _dsn() -> str:
    raw = os.environ.get("CB_PG_DSN", "postgresql://cookiebot:cookiebot@localhost:5432/cookiebot")
    # asyncpg DSN -> SQLAlchemy/psycopg URL
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw


# The startup runner (cb_core.migrations) builds a Config in memory and sets the
# URL itself; only fall back to the environment when invoked from the CLI.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", _dsn())

target_metadata = None  # raw-SQL migrations; nothing to autogenerate against


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _shard_count(connection: Connection) -> None:
    """Fix the shard count for every table these migrations distribute.

    Set on the migration session rather than on the server, so local, CI and
    production agree no matter how Postgres was started.

    8, not Citus's default 32: each shard is a real table, and each table adds a
    composite type to the catalog. At 32 a single node carried ~1000 of them,
    which is enough to make asyncpg's recursive `typeinfo_tree` introspection —
    it aggregates over pg_attribute once per composite type — take seconds on the
    first array-typed statement of a connection. Raise it when the cluster has
    more than a couple of nodes; `alter_distributed_table(..., shard_count => N)`
    re-shards without a schema change.
    """
    count = os.environ.get("CB_CITUS_SHARD_COUNT", "8")
    if not count.isdigit() or not (1 <= int(count) <= 4096):
        raise ValueError(f"CB_CITUS_SHARD_COUNT must be 1..4096, got {count!r}")
    if connection.exec_driver_sql(
        "SELECT count(*) FROM pg_settings WHERE name = 'citus.shard_count'"
    ).scalar():
        connection.exec_driver_sql(f"SET citus.shard_count TO {int(count)}")


def _sequential_multi_shard(connection: Connection) -> None:
    """One connection per node for the whole migration run.

    Alembic runs every revision in a single transaction, so a downgrade drops
    distributed tables and then drops a distributed function in the same
    transaction. Citus refuses that in its default parallel mode:

        cannot run function command because there was a parallel operation on a
        distributed table in the transaction

    Sequential mode is the documented fix. Migrations are DDL on an idle
    database, so losing cross-shard parallelism costs nothing here.

    Skipped on a plain Postgres — the GUC only exists when Citus is preloaded.
    """
    has_guc = connection.exec_driver_sql(
        "SELECT count(*) FROM pg_settings WHERE name = 'citus.multi_shard_modify_mode'"
    ).scalar()
    if has_guc:
        connection.exec_driver_sql("SET citus.multi_shard_modify_mode TO 'sequential'")


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            # Must be inside alembic's transaction, not before it: any statement
            # run first starts an implicit SQLAlchemy transaction, alembic then
            # sees itself as already in one, its begin_transaction() becomes a
            # no-op, and the whole migration is rolled back at close — silently,
            # with every "Running upgrade" line still printed.
            _sequential_multi_shard(connection)
            _shard_count(connection)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
