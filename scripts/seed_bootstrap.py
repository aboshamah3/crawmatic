#!/usr/bin/env python3
"""seed_bootstrap.py — administrative bootstrap seed (SPEC-03 T049, FR-004/FR-023).

There is no self-service signup endpoint (spec Assumptions / research
D6): the very first ``workspaces`` row and the very first
``SUPER_ADMIN`` ``users`` row are created by running this script once,
by hand, against the **direct** privileged connection
(``Settings.MIGRATION_DATABASE_URL`` — the same direct-to-Postgres URL
``alembic/env.py`` uses, never the PgBouncer pooler / ``DATABASE_URL``).
That connection is a superuser-ish role with no ``FORCE ROW LEVEL
SECURITY`` restriction in play the way the pooled app role is, so this
script's writes are not subject to the ``users``/``api_keys`` RLS
policies the way ordinary request traffic is (contracts/
migration-identity.md "Bootstrap seed").

Reads from the environment:

* ``BOOTSTRAP_ADMIN_EMAIL`` (required) — the SUPER_ADMIN's login email.
* ``BOOTSTRAP_ADMIN_PASSWORD`` (required) — hashed with
  :func:`app_shared.security.passwords.hash_password` (argon2id); the
  plaintext is never persisted or logged.
* ``BOOTSTRAP_WORKSPACE_NAME`` (optional, default ``"Default
  Workspace"``) / ``BOOTSTRAP_WORKSPACE_SLUG`` (optional, default
  ``"default"``) — used only when no workspace exists yet.

Idempotent: re-running with the same (or different) env values never
creates a second workspace once one exists, and never creates a second
user for an email that already exists — it reports what already exists
and exits 0.

Importable without connecting to anything: constructing the engine and
opening a session only happens inside :func:`main`, never at import
time — a plain ``import scripts.seed_bootstrap`` (or running this
module's unit test) does no I/O at all. See ``tests/unit/
test_seed_bootstrap.py`` for the DB-independent assertions; the live
round-trip (``alembic upgrade head`` then this script, on a real
Postgres) is ``tests/integration/test_seed_bootstrap.py`` (deferred,
T050).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# `app_shared` is a uv-workspace member (libs/shared) installed into the
# venv like any other dependency — no sys.path manipulation needed, same
# as `scripts/check_workspace_scoping.py`.
from app_shared.enums import UserRole, UserStatus, WorkspaceStatus
from app_shared.models.identity import User, Workspace
from app_shared.security.passwords import hash_password

DEFAULT_WORKSPACE_NAME = "Default Workspace"
DEFAULT_WORKSPACE_SLUG = "default"


@dataclass(frozen=True)
class BootstrapConfig:
    """Resolved bootstrap inputs (env-sourced, validated up front)."""

    admin_email: str
    admin_password: str
    workspace_name: str
    workspace_slug: str


class BootstrapConfigError(RuntimeError):
    """Raised when required bootstrap environment variables are missing."""


def load_config(env: dict[str, str] | None = None) -> BootstrapConfig:
    """Read + validate the bootstrap env vars.

    ``BOOTSTRAP_ADMIN_EMAIL``/``BOOTSTRAP_ADMIN_PASSWORD`` are required —
    a missing value raises :class:`BootstrapConfigError` immediately
    (fail fast) rather than silently seeding nothing. ``env`` defaults to
    ``os.environ`` and is injectable for tests.
    """
    source = os.environ if env is None else env
    email = source.get("BOOTSTRAP_ADMIN_EMAIL", "").strip()
    password = source.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email:
        raise BootstrapConfigError("BOOTSTRAP_ADMIN_EMAIL is required and was not set.")
    if not password:
        raise BootstrapConfigError("BOOTSTRAP_ADMIN_PASSWORD is required and was not set.")

    workspace_name = source.get("BOOTSTRAP_WORKSPACE_NAME", "").strip() or DEFAULT_WORKSPACE_NAME
    workspace_slug = source.get("BOOTSTRAP_WORKSPACE_SLUG", "").strip() or DEFAULT_WORKSPACE_SLUG

    return BootstrapConfig(
        admin_email=email,
        admin_password=password,
        workspace_name=workspace_name,
        workspace_slug=workspace_slug,
    )


def resolve_migration_db_url(env: dict[str, str] | None = None) -> str:
    """Resolve the direct-to-Postgres URL this script must connect through.

    Mirrors ``alembic/env.py``'s ``_resolve_db_url``: prefer
    ``Settings.MIGRATION_DATABASE_URL`` (via ``get_settings()``), but
    fall back to reading ``MIGRATION_DATABASE_URL`` straight from the
    environment if ``Settings()`` can't construct (e.g. a minimal
    bootstrap host that hasn't set the unrelated required app vars like
    ``DATABASE_URL``/``REDIS_URL``). Never falls back to the pooled
    ``DATABASE_URL`` — the pooler role is not privileged enough to
    bypass RLS for this one-time seed. Raises :class:`BootstrapConfigError`
    if no URL can be resolved either way.
    """
    source = os.environ if env is None else env

    url: str | None = None
    try:
        from app_shared.config import get_settings

        url = get_settings().MIGRATION_DATABASE_URL
    except Exception:
        url = None

    if not url:
        url = source.get("MIGRATION_DATABASE_URL")

    if not url:
        raise BootstrapConfigError(
            "MIGRATION_DATABASE_URL is required (direct-to-Postgres, privileged "
            "connection) to run the bootstrap seed."
        )
    return url


def build_engine(database_url: str) -> Engine:
    """Build the direct (non-pooled) engine used for the bootstrap seed.

    Same ``prepare_threshold=None`` convention as
    ``app_shared.database`` — harmless here (this URL targets Postgres
    directly, not PgBouncer) but keeps the driver configuration
    consistent across every entry point that talks to Postgres.
    """
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"prepare_threshold": None},
    )


def ensure_workspace(session: Session, name: str, slug: str) -> tuple[Workspace, bool]:
    """Return the first workspace, creating it if none exists yet.

    "First" is deliberately "any workspace at all" (not "a workspace
    with this slug"): this script is the *only* writer of ``workspaces``
    rows in SPEC-03 (no workspace CRUD endpoint exists yet), so once one
    workspace exists the bootstrap step is done, regardless of what
    ``BOOTSTRAP_WORKSPACE_NAME``/``_SLUG`` a re-run happens to pass.
    Returns ``(workspace, created)``.
    """
    existing = session.execute(select(Workspace).order_by(Workspace.created_at)).scalars().first()
    if existing is not None:
        return existing, False

    workspace = Workspace(name=name, slug=slug, status=WorkspaceStatus.ACTIVE)
    session.add(workspace)
    session.flush()
    return workspace, True


def ensure_super_admin(session: Session, email: str, password: str) -> tuple[User, bool]:
    """Return the SUPER_ADMIN user for ``email``, creating it if absent.

    Looked up by ``email`` (unique constraint) rather than "any
    SUPER_ADMIN exists" so re-running with a different
    ``BOOTSTRAP_ADMIN_EMAIL`` after the first admin already exists is
    still safe (no duplicate insert for the *same* email; this script
    does not attempt to prevent a deliberate second admin with a
    different email). Returns ``(user, created)``.
    """
    existing = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return existing, False

    user = User(
        workspace_id=None,
        email=email,
        password_hash=hash_password(password),
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    session.flush()
    return user, True


def workspace_count(session: Session) -> int:
    """Return the total number of ``workspaces`` rows (used for reporting/tests)."""
    return int(session.execute(select(func.count()).select_from(Workspace)).scalar_one())


@dataclass(frozen=True)
class SeedResult:
    """Outcome of one :func:`run_seed` call, for reporting and tests."""

    workspace_id: object
    workspace_created: bool
    admin_user_id: object
    admin_email: str
    admin_created: bool


def run_seed(session: Session, config: BootstrapConfig) -> SeedResult:
    """Perform the idempotent seed on an already-open ``session``.

    Does not commit — the caller controls the transaction boundary (see
    :func:`main`), which keeps this function trivially testable against
    a fake/mock session.
    """
    workspace, workspace_created = ensure_workspace(
        session, config.workspace_name, config.workspace_slug
    )
    user, admin_created = ensure_super_admin(session, config.admin_email, config.admin_password)
    return SeedResult(
        workspace_id=workspace.id,
        workspace_created=workspace_created,
        admin_user_id=user.id,
        admin_email=user.email,
        admin_created=admin_created,
    )


def _report(result: SeedResult) -> str:
    lines = [
        f"workspace: {'created' if result.workspace_created else 'already present'} "
        f"(id={result.workspace_id})",
        f"admin user {result.admin_email}: "
        f"{'created' if result.admin_created else 'already present'} "
        f"(id={result.admin_user_id})",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point: resolve config + URL, connect, seed, commit, report.

    All I/O (config validation aside) happens here — nothing above this
    function touches the network or a database, so importing this
    module is always safe.
    """
    del argv  # no CLI flags today; env-var driven per the contract.

    try:
        config = load_config()
        database_url = resolve_migration_db_url()
    except BootstrapConfigError as exc:
        print(f"seed_bootstrap: FAIL — {exc}", file=sys.stderr)
        return 1

    engine = build_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    try:
        result = run_seed(session, config)
        session.commit()
    except Exception as exc:  # pragma: no cover - defensive, real-DB path only
        session.rollback()
        print(f"seed_bootstrap: FAIL — {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()

    print("seed_bootstrap: OK")
    print(_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
