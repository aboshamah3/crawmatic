"""Unit tests for `scripts/seed_bootstrap.py` (SPEC-03 T049, FR-004/FR-023).

DB-independent: no real Postgres connection is ever opened here.
`ensure_workspace`/`ensure_super_admin`/`run_seed` are exercised against
a tiny hand-rolled fake `Session` (not `sqlalchemy.orm.Session`) that
returns pre-seeded rows rather than compiling/executing real SQL — this
is deliberately a thin stub (no WHERE-clause evaluation), sufficient to
prove the idempotency *branching* (create-if-absent vs. return-existing)
without a database. The live round-trip against a real Postgres is
`tests/integration/test_seed_bootstrap.py` (T050, deferred).

Also covers: the module is importable with zero environment variables
set and without opening any connection (`build_engine`/`main` are the
only functions that ever touch the network, and only when called);
`load_config`/`resolve_migration_db_url` fail fast with a clear error
when required env vars are missing; `main` wires config -> engine ->
session -> seed -> commit using a stubbed `create_engine`/`sessionmaker`
(captured, never a real connection).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# `scripts/` has no __init__.py / installed entry point -- match the
# sys.path convention `tests/unit/test_workspace_scoping_guard.py` uses
# to import `scripts.check_workspace_scoping`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.seed_bootstrap as seed_bootstrap  # noqa: E402
from app_shared.enums import UserRole, UserStatus, WorkspaceStatus  # noqa: E402
from app_shared.models.identity import User, Workspace  # noqa: E402
from scripts.seed_bootstrap import (  # noqa: E402
    BootstrapConfig,
    BootstrapConfigError,
    ensure_super_admin,
    ensure_workspace,
    load_config,
    resolve_migration_db_url,
    run_seed,
)
import app_shared.security.passwords as passwords_module  # noqa: E402


class _FakeArgon2Settings:
    """Minimal Settings stand-in exposing only the ARGON2_* fields
    `hash_password` reads (same pattern as `tests/unit/test_passwords.py`) --
    avoids requiring the full required-env `Settings` (DATABASE_URL/
    REDIS_URL/... are irrelevant to hashing and unset in this test process).
    """

    ARGON2_TIME_COST: int | None = None
    ARGON2_MEMORY_COST: int | None = None
    ARGON2_PARALLELISM: int | None = None


@pytest.fixture(autouse=True)
def _patch_argon2_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(passwords_module, "get_settings", lambda: _FakeArgon2Settings())
    monkeypatch.setattr(passwords_module, "_dummy_hash", None, raising=False)


# --- import safety -----------------------------------------------------


def test_module_is_importable_with_no_env_vars_and_does_no_io() -> None:
    """Importing the module (already done at collection time, with zero
    ``BOOTSTRAP_*``/``MIGRATION_DATABASE_URL`` env vars required to get this
    far) exposes the entry point and never eagerly built an engine/session.

    Deliberately does NOT ``importlib.reload()`` here: reloading would
    rebind ``scripts.seed_bootstrap``'s module-global ``BootstrapConfigError``
    to a *new* class object in place (same ``__dict__``, mutated), so every
    already-imported function in this test file (whose ``__globals__`` is
    that same dict) would start raising the reloaded class while the
    ``BootstrapConfigError`` name imported at the top of this file keeps
    pointing at the pre-reload class -- a self-inflicted identity mismatch
    that breaks every ``pytest.raises(BootstrapConfigError, ...)`` below.
    """
    assert hasattr(seed_bootstrap, "main")
    assert callable(seed_bootstrap.main)


# --- load_config ---------------------------------------------------------


def test_load_config_requires_admin_email() -> None:
    with pytest.raises(BootstrapConfigError, match="BOOTSTRAP_ADMIN_EMAIL"):
        load_config({"BOOTSTRAP_ADMIN_PASSWORD": "x"})


def test_load_config_requires_admin_password() -> None:
    with pytest.raises(BootstrapConfigError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        load_config({"BOOTSTRAP_ADMIN_EMAIL": "admin@example.com"})


def test_load_config_applies_workspace_defaults_when_unset() -> None:
    config = load_config(
        {"BOOTSTRAP_ADMIN_EMAIL": "admin@example.com", "BOOTSTRAP_ADMIN_PASSWORD": "hunter2"}
    )
    assert config.workspace_name == seed_bootstrap.DEFAULT_WORKSPACE_NAME
    assert config.workspace_slug == seed_bootstrap.DEFAULT_WORKSPACE_SLUG


def test_load_config_honors_explicit_workspace_overrides() -> None:
    config = load_config(
        {
            "BOOTSTRAP_ADMIN_EMAIL": "admin@example.com",
            "BOOTSTRAP_ADMIN_PASSWORD": "hunter2",
            "BOOTSTRAP_WORKSPACE_NAME": "Acme HQ",
            "BOOTSTRAP_WORKSPACE_SLUG": "acme-hq",
        }
    )
    assert config.workspace_name == "Acme HQ"
    assert config.workspace_slug == "acme-hq"


# --- resolve_migration_db_url ---------------------------------------------


def test_resolve_migration_db_url_reads_env_when_settings_unavailable() -> None:
    # get_settings() will fail to construct here (required app vars like
    # DATABASE_URL/REDIS_URL are not set in this test process) -- must
    # fall back to reading MIGRATION_DATABASE_URL straight from `env`.
    url = resolve_migration_db_url({"MIGRATION_DATABASE_URL": "postgresql://x/y"})
    assert url == "postgresql://x/y"


def test_resolve_migration_db_url_raises_when_unset() -> None:
    with pytest.raises(BootstrapConfigError, match="MIGRATION_DATABASE_URL"):
        resolve_migration_db_url({})


# --- fake session for ensure_workspace / ensure_super_admin / run_seed ---


class _FakeScalarResult:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def first(self) -> object | None:
        return self._items[0] if self._items else None


class _FakeExecuteResult:
    """Stands in for `sqlalchemy.engine.Result` for exactly the two calls
    `ensure_workspace`/`ensure_super_admin` make: `.scalars().first()` and
    `.scalar_one_or_none()`. See module docstring for why this doesn't
    evaluate real SQL.
    """

    def __init__(self, items: list[object]) -> None:
        self._items = items

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._items)

    def scalar_one_or_none(self) -> object | None:
        return self._items[0] if self._items else None


class FakeSession:
    """Minimal stand-in for `sqlalchemy.orm.Session`.

    Routes `execute()` by inspecting which mapped entity the `select()`
    targets (`Workspace` vs `User`) via `column_descriptions` and returns
    whatever this fake currently holds for that entity -- it does not
    evaluate WHERE clauses. Tests control existence purely by what they
    pre-populate `workspaces`/`users` with before calling.
    """

    def __init__(self, workspaces: list[Workspace] | None = None, users: list[User] | None = None) -> None:
        self.workspaces: list[Workspace] = list(workspaces or [])
        self.users: list[User] = list(users or [])
        self.added: list[object] = []

    def execute(self, stmt: object) -> _FakeExecuteResult:
        entity = stmt.column_descriptions[0].get("entity")  # type: ignore[attr-defined]
        if entity is Workspace:
            return _FakeExecuteResult(self.workspaces)
        if entity is User:
            return _FakeExecuteResult(self.users)
        raise AssertionError(f"FakeSession.execute: unhandled entity {entity!r}")

    def add(self, obj: object) -> None:
        self.added.append(obj)
        if isinstance(obj, Workspace):
            self.workspaces.append(obj)
        elif isinstance(obj, User):
            self.users.append(obj)

    def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()  # type: ignore[attr-defined]


# --- ensure_workspace ------------------------------------------------------


def test_ensure_workspace_creates_when_none_exists() -> None:
    session = FakeSession()
    workspace, created = ensure_workspace(session, "Default Workspace", "default")

    assert created is True
    assert workspace.name == "Default Workspace"
    assert workspace.slug == "default"
    assert workspace.status == WorkspaceStatus.ACTIVE
    assert workspace.id is not None
    assert workspace in session.workspaces


def test_ensure_workspace_returns_existing_without_creating_a_second() -> None:
    existing = Workspace(name="Already Here", slug="already-here", status=WorkspaceStatus.ACTIVE)
    existing.id = uuid.uuid4()
    session = FakeSession(workspaces=[existing])

    workspace, created = ensure_workspace(session, "Default Workspace", "default")

    assert created is False
    assert workspace is existing
    assert session.added == []
    assert len(session.workspaces) == 1


# --- ensure_super_admin ------------------------------------------------------


def test_ensure_super_admin_creates_when_absent() -> None:
    session = FakeSession()
    user, created = ensure_super_admin(session, "admin@example.com", "hunter22222")

    assert created is True
    assert user.email == "admin@example.com"
    assert user.workspace_id is None
    assert user.role == UserRole.SUPER_ADMIN
    assert user.status == UserStatus.ACTIVE
    # argon2id-hashed, never the raw plaintext.
    assert user.password_hash != "hunter22222"
    assert user.id is not None
    assert user in session.users


def test_ensure_super_admin_returns_existing_without_creating_a_second() -> None:
    existing = User(
        workspace_id=None,
        email="admin@example.com",
        password_hash="already-hashed",
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
    )
    existing.id = uuid.uuid4()
    session = FakeSession(users=[existing])

    user, created = ensure_super_admin(session, "admin@example.com", "hunter22222")

    assert created is False
    assert user is existing
    assert session.added == []
    assert len(session.users) == 1


# --- run_seed (both steps together) -----------------------------------------


def test_run_seed_creates_workspace_and_admin_on_a_fresh_database() -> None:
    session = FakeSession()
    config = BootstrapConfig(
        admin_email="admin@example.com",
        admin_password="hunter22222",
        workspace_name="Default Workspace",
        workspace_slug="default",
    )

    result = run_seed(session, config)

    assert result.workspace_created is True
    assert result.admin_created is True
    assert result.admin_email == "admin@example.com"
    assert len(session.workspaces) == 1
    assert len(session.users) == 1


def test_run_seed_is_idempotent_when_both_already_exist() -> None:
    existing_workspace = Workspace(name="W", slug="w", status=WorkspaceStatus.ACTIVE)
    existing_workspace.id = uuid.uuid4()
    existing_user = User(
        workspace_id=None,
        email="admin@example.com",
        password_hash="already-hashed",
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
    )
    existing_user.id = uuid.uuid4()
    session = FakeSession(workspaces=[existing_workspace], users=[existing_user])
    config = BootstrapConfig(
        admin_email="admin@example.com",
        admin_password="hunter22222",
        workspace_name="Default Workspace",
        workspace_slug="default",
    )

    result = run_seed(session, config)

    assert result.workspace_created is False
    assert result.admin_created is False
    assert result.workspace_id == existing_workspace.id
    assert result.admin_user_id == existing_user.id
    assert len(session.workspaces) == 1
    assert len(session.users) == 1


# --- main() wiring (stubbed engine/session -- never a real connection) -----


def test_main_returns_nonzero_and_prints_error_when_config_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)

    exit_code = seed_bootstrap.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "BOOTSTRAP_ADMIN_EMAIL" in captured.err


def test_main_never_opens_a_real_connection_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "hunter22222")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://unused/never-connected")

    fake_session = FakeSession()
    captured_engine_args: dict[str, object] = {}

    class _FakeEngine:
        def dispose(self) -> None:
            pass

    def _fake_create_engine(url: str, **kwargs: object) -> _FakeEngine:
        captured_engine_args["url"] = url
        captured_engine_args["kwargs"] = kwargs
        return _FakeEngine()

    def _fake_sessionmaker(*, bind: object, expire_on_commit: bool) -> object:
        def _factory() -> FakeSession:
            return fake_session

        return _factory

    # Fake session needs commit()/close() no-ops for main()'s flow.
    fake_session.commit = lambda: None  # type: ignore[attr-defined]
    fake_session.rollback = lambda: None  # type: ignore[attr-defined]
    fake_session.close = lambda: None  # type: ignore[attr-defined]

    monkeypatch.setattr(seed_bootstrap, "create_engine", _fake_create_engine)
    monkeypatch.setattr(seed_bootstrap, "sessionmaker", _fake_sessionmaker)

    exit_code = seed_bootstrap.main([])

    assert exit_code == 0
    assert captured_engine_args["url"] == "postgresql://unused/never-connected"
    out = capsys.readouterr().out
    assert "seed_bootstrap: OK" in out
    assert "admin@example.com" in out
    assert len(fake_session.workspaces) == 1
    assert len(fake_session.users) == 1
