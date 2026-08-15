"""Runtime build/version/migration-provenance endpoint.

Audit ref: `CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md` §C2
("Release source of truth is not controlled") — required fix #2: "expose
build/version information in an operational endpoint or deployment
metadata," so a correct local fix can't silently be absent from what is
actually deployed, and a green test run can be tied to the exact SHA +
migration head that produced it.

`GET /version` — unauthenticated, same posture as `/health` (an
ops-facing diagnostic surface has to be curl-able without a bearer
credential to be useful during an incident) but, unlike `/health`
(SPEC-01, contracts/health.md — MUST NOT touch the database), this
endpoint DOES read the database: that is the whole point. It queries
exactly one system table (`alembic_version`) — never tenant/workspace
data — so it needs no auth seam and no `app.workspace_id` RLS GUC, and
it leaks no secret (no connection string, no credential, no stack
trace — DB failures are reported as an exception class name only).

Reports:

* ``git_sha`` — the deployed commit, from ``GIT_SHA`` (our own
  build-time env, set by `.github/workflows/ci.yml`'s `images` job) or
  ``RAILWAY_GIT_COMMIT_SHA`` (injected automatically by Railway on every
  deploy), falling back to ``"unknown"`` rather than ever raising.
* ``build_time`` — ``BUILD_TIMESTAMP`` (our own build-time env) or
  Railway's ``RAILWAY_DEPLOYMENT_ID`` as a weaker fallback correlator,
  else ``null``.
* ``code_migration_head`` — resolved from `alembic.ini` +
  `alembic/versions/*.py` via `alembic.script.ScriptDirectory`, exactly
  what `scripts/check_single_head.sh` resolves (DB-independent).
* ``db_migration_head`` — the live database's `alembic_version.version_num`.
* ``migration_heads_match`` — ``True``/``False`` when both are known,
  ``null`` when either side couldn't be resolved (never a guess).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.database import get_session

router = APIRouter(tags=["version"])

# apps/api/app/routers/version.py -> apps/api/app -> apps/api -> apps -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _get_db_session() -> Iterator[Session]:
    """A bare, no-workspace-context DB session.

    Deliberately NOT `app.deps.get_current_principal`/`require_scopes`:
    this endpoint reads one system table, not tenant-owned data, so no
    auth seam and no RLS GUC are needed. A plain FastAPI dependency
    (rather than calling `get_session()` inline) so tests can override it
    with `app.dependency_overrides` the same way every other router's DB
    dependency is overridden.
    """
    with get_session() as session:
        yield session


class VersionResponse(BaseModel):
    git_sha: str
    build_time: str | None
    code_migration_head: str | None
    db_migration_head: str | None
    migration_heads_match: bool | None
    db_error: str | None = None


def _git_sha() -> str:
    return os.environ.get("GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "unknown"


def _build_time() -> str | None:
    return os.environ.get("BUILD_TIMESTAMP") or os.environ.get("RAILWAY_DEPLOYMENT_ID") or None


def _code_migration_head() -> str | None:
    """The single migration head `alembic/versions/*.py` resolves to.

    Returns ``None`` (never raises) when `alembic.ini` is missing or the
    revision graph can't be resolved (e.g. a diverged/forked history —
    `scripts/check_single_head.sh` is the hard CI gate for that; this
    endpoint just surfaces what it can).
    """
    if not _ALEMBIC_INI.exists():
        return None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(_ALEMBIC_INI))
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()
    except Exception:
        return None


@router.get("/version", response_model=VersionResponse)
def version(session: Session = Depends(_get_db_session)) -> VersionResponse:
    code_head = _code_migration_head()
    db_head: str | None = None
    db_error: str | None = None
    try:
        row = session.execute(text("SELECT version_num FROM alembic_version")).first()
        db_head = row[0] if row else None
    except Exception as exc:  # pragma: no cover - defensive: DB down/unreachable/table absent
        db_error = exc.__class__.__name__

    migration_heads_match = (
        None if (code_head is None or db_head is None) else code_head == db_head
    )

    return VersionResponse(
        git_sha=_git_sha(),
        build_time=_build_time(),
        code_migration_head=code_head,
        db_migration_head=db_head,
        migration_heads_match=migration_heads_match,
        db_error=db_error,
    )
