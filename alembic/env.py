"""Alembic environment: shared metadata + direct-to-Postgres migration engine.

Per ``contracts/migration-job.md`` / research.md D6:

* ``target_metadata`` is the single shared ``app_shared.models.metadata``
  object — the same ``MetaData`` every ORM model (including the demo
  ``_smoke_foundation`` table, imported transitively via
  ``app_shared.models``) is declared against, so autogenerate and
  offline/online rendering both honor the naming convention (FR-014).
* ``compare_type = True`` so autogenerate detects type drift (e.g.
  ``TIMESTAMPTZ`` vs. naive ``TIMESTAMP``, ``NUMERIC`` scale changes).
* The online engine is built from ``Settings.MIGRATION_DATABASE_URL``
  (direct-to-Postgres — e.g. ``postgres:5432``), never from
  ``app_shared.database.get_engine()`` (which targets the PgBouncer
  pooler at ``pgbouncer:6432``). Alembic's version-table advisory lock
  and any future ``CREATE INDEX CONCURRENTLY`` are unsafe under
  transaction pooling, so migrations always connect directly.
* A CLI ``-x db_url=<url>`` override takes precedence over
  ``MIGRATION_DATABASE_URL`` (useful for one-off runs against a URL not
  present in the environment, e.g. from a test fixture).
* Offline mode (``alembic upgrade head --sql``) renders DDL from
  ``target_metadata`` with ``literal_binds`` and no DB connection at
  all — this is what runs in this build environment (no Docker
  daemon / no live Postgres).
* Running online without a resolvable URL fails fast with a clear
  error instead of silently falling back to the pooler.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app_shared.models  # noqa: F401 - registers all tables (incl. _smoke) on metadata
from app_shared.config import get_settings

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Interpret the config file for Python logging (alembic.ini's
# [loggers]/[handlers]/[formatters] sections), if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single shared metadata — every model (base mixins + _smoke_foundation)
# is declared against this MetaData, so autogenerate/render honors the
# NAMING_CONVENTION (FR-014).
target_metadata = app_shared.models.metadata


def _resolve_db_url() -> str | None:
    """Resolve the direct-to-Postgres migration URL.

    Precedence: ``-x db_url=<url>`` CLI override, then
    ``Settings.MIGRATION_DATABASE_URL``. Deliberately never falls back to
    ``DATABASE_URL`` (the PgBouncer pooler) — migrations must always
    connect directly (contracts/migration-job.md).
    """
    x_args = context.get_x_argument(as_dictionary=True)
    cli_url = x_args.get("db_url")
    if cli_url:
        return cli_url

    try:
        settings = get_settings()
    except Exception:
        # Settings() can fail to construct if other required env vars
        # (DATABASE_URL, REDIS_URL, ...) are absent, e.g. when only
        # MIGRATION_DATABASE_URL is set in a minimal migration-job
        # environment. Fall back to reading it directly from the
        # environment in that case.
        import os

        return os.environ.get("MIGRATION_DATABASE_URL")

    return settings.MIGRATION_DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (renders SQL, no DB connection).

    Configures the context with just a URL (which may be absent — no
    connection is ever attempted) and ``literal_binds`` so values are
    rendered directly into the SQL text instead of being bound
    parameters. This is the mode exercised in this build environment via
    ``alembic upgrade head --sql``.
    """
    url = _resolve_db_url() or "postgresql+psycopg://offline:offline@localhost:5432/offline"
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (opens a real DB connection).

    Builds its own engine directly from ``MIGRATION_DATABASE_URL`` —
    never ``app_shared.database.get_engine()`` (pooler-bound). Errors
    clearly if no URL is resolvable so a misconfigured migration job
    fails fast instead of silently doing nothing.
    """
    url = _resolve_db_url()
    if not url:
        raise RuntimeError(
            "No database URL available for online migrations. Set "
            "MIGRATION_DATABASE_URL (direct-to-Postgres, e.g. "
            "postgresql+psycopg://user:pass@postgres:5432/db) or pass "
            "-x db_url=<url> to alembic. Never falls back to DATABASE_URL "
            "(the PgBouncer pooler) — migrations must connect directly."
        )

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
