"""One-shot Alembic migration job (SPEC-02 US1, contracts/migration-job.md).

This package exists only so ``apps/migrate`` is a real uv-workspace
member with its own pinned dependency set (``alembic`` + ``app_shared``,
resolved from the root lockfile — see ``uv.lock``). It carries no
runtime code of its own: the migration job's Dockerfile ``CMD`` runs
``alembic upgrade head`` directly against the repo-root ``alembic.ini``
/ ``alembic/env.py``, which drive the actual migration via
``app_shared.models.metadata`` and ``Settings.MIGRATION_DATABASE_URL``
(direct-to-Postgres, never the PgBouncer pooler).
"""

from __future__ import annotations
