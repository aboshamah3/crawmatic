"""Every `refresh_tokens` statement runs on the BYPASSRLS auth seam (EPA B8b).

READY-007 / P0.5. `refresh_tokens` gained transitive row-level security
through `users.user_id` at alembic head `b6d94c2f1a70`. That policy is
fail-closed on an unset `app.workspace_id`, and **none** of the three
statements `apps/api/app/routers/auth.py` issues against the table can
supply one:

* rotate (`POST /v1/auth/refresh`) and revoke (`POST /v1/auth/logout`)
  are keyed by an unforgeable `token_hash` and are what *resolve* the
  principal — there is no workspace context yet;
* issue (`_issue_pair`) knows the user, but a `SUPER_ADMIN` has
  `workspace_id IS NULL` and satisfies no context at all.

So all three moved onto `get_auth_session()`. The failure this file
exists to catch is silent and total: on the ordinary app-role session
those statements would match **zero rows** under the new policy, and the
API would answer every refresh with the uniform "wrong credentials"
error and every logout with a 204 that revoked nothing. Nothing raises;
the tests that mock the database would still pass.

Pure unit level — no database, no `.env`, no live services. The sessions
are fakes and the only thing asserted is *which* seam each statement was
handed to.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.elements import TextClause

from app.routers import auth as auth_router
from app_shared.enums import UserRole, UserStatus
from app_shared.models.identity import RefreshToken, User


class _FakeResult:
    """Stands in for whatever `Session.execute` returned for one statement."""

    def __init__(self, mapping: dict | None = None, scalar: object | None = None) -> None:
        self._mapping = mapping
        self._scalar = scalar

    def mappings(self) -> _FakeResult:
        return self

    def first(self) -> dict | None:
        return self._mapping

    def scalar_one_or_none(self) -> object | None:
        return self._scalar


class _FakeSession:
    """Records every statement and every added ORM object."""

    def __init__(self, label: str, *, rotated: dict | None, user: User | None) -> None:
        self.label = label
        self.statements: list[str] = []
        self.added: list[object] = []
        self.commits = 0
        self._rotated = rotated
        self._user = user

    def execute(self, statement, params=None):  # noqa: ANN001 - test double
        self.statements.append(str(statement))
        if isinstance(statement, TextClause):
            return _FakeResult(mapping=self._rotated)
        return _FakeResult(scalar=self._user)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        return None


class _Seams:
    """Hands out fake sessions and remembers which seam opened each one."""

    def __init__(self, *, rotated: dict | None = None, user: User | None = None) -> None:
        self.rotated = rotated
        self.user = user
        self.opened: list[_FakeSession] = []

    def _factory(self, label: str):
        @contextmanager
        def _open():
            session = _FakeSession(label, rotated=self.rotated, user=self.user)
            self.opened.append(session)
            try:
                yield session
            finally:
                session.close()

        return _open

    def statements_on(self, label: str) -> list[str]:
        return [s for session in self.opened if session.label == label for s in session.statements]

    def added_on(self, label: str) -> list[object]:
        return [o for session in self.opened if session.label == label for o in session.added]


@pytest.fixture()
def user() -> User:
    return User(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        email="seam@example.test",
        password_hash="not-a-real-hash",
        role=UserRole.WORKSPACE_ADMIN,
        status=UserStatus.ACTIVE,
    )


@pytest.fixture()
def seams(monkeypatch: pytest.MonkeyPatch, user: User) -> _Seams:
    """Replace both session seams and the settings/JWT the issue path needs.

    `get_settings` is stubbed rather than built: this suite must never
    read a `.env`, and the property under test is which seam the
    statements land on, not how a DSN or a signing key is resolved.
    """
    seams = _Seams(
        rotated={"id": uuid.uuid4(), "user_id": user.id},
        user=user,
    )
    monkeypatch.setattr(auth_router, "get_auth_session", seams._factory("auth"))
    # The app-role seam is installed under its usual name so a regression
    # that reaches for it is RECORDED rather than raising an
    # AttributeError that a reader could mistake for an unrelated bug.
    monkeypatch.setattr(auth_router, "get_session", seams._factory("app"), raising=False)
    monkeypatch.setattr(
        auth_router,
        "get_settings",
        lambda: SimpleNamespace(
            JWT_SECRET="unit-test-secret",  # noqa: S106 - no signing happens
            JWT_ALGORITHM="HS256",
            ACCESS_TOKEN_TTL_SECONDS=900,
            REFRESH_TOKEN_TTL_SECONDS=1209600,
        ),
    )
    monkeypatch.setattr(auth_router, "encode_access_token", lambda **_: "access-token")
    return seams


# =====================================================================
# The three statements
# =====================================================================


def test_refresh_rotation_runs_on_the_auth_seam(seams: _Seams) -> None:
    """The pre-auth rotation. On the app role this would match zero rows."""
    auth_router.refresh(auth_router.RefreshRequest(refresh_token="presented-raw-token"))

    rotations = [s for s in seams.statements_on("auth") if "UPDATE refresh_tokens" in s]
    assert rotations, "the rotation UPDATE did not run on get_auth_session()"
    assert "RETURNING id, user_id" in rotations[0]
    assert seams.statements_on("app") == [], (
        "a refresh_tokens statement ran on the RLS-confined app-role session — under "
        "the transitive policy it would silently match zero rows and every refresh "
        "would fail closed as a bad token"
    )


def test_refresh_issues_the_new_token_on_the_auth_seam(seams: _Seams, user: User) -> None:
    """The INSERT too: a SUPER_ADMIN's NULL workspace satisfies no WITH CHECK."""
    pair = auth_router.refresh(auth_router.RefreshRequest(refresh_token="presented-raw-token"))

    inserted = [o for o in seams.added_on("auth") if isinstance(o, RefreshToken)]
    assert len(inserted) == 1, "the replacement refresh token was not inserted on the auth seam"
    assert inserted[0].user_id == user.id
    assert seams.added_on("app") == []
    # The raw token is returned to the caller; only its hash is persisted.
    assert pair.refresh_token
    assert inserted[0].token_hash != pair.refresh_token


def test_logout_revocation_runs_on_the_auth_seam(seams: _Seams) -> None:
    """A logout that revoked nothing would still answer 204 — hence this test."""
    auth_router.logout(auth_router.LogoutRequest(refresh_token="presented-raw-token"))

    revocations = [
        s
        for s in seams.statements_on("auth")
        if "UPDATE refresh_tokens" in s and "RETURNING" not in s
    ]
    assert revocations, "the revocation UPDATE did not run on get_auth_session()"
    assert seams.statements_on("app") == []


def test_rotation_miss_is_rejected_and_issues_nothing(monkeypatch, user: User) -> None:
    """Zero rows from the rotation must still reject — the policy must not mask reuse."""
    from fastapi import HTTPException  # noqa: PLC0415 - local to this assertion

    seams = _Seams(rotated=None, user=user)
    monkeypatch.setattr(auth_router, "get_auth_session", seams._factory("auth"))

    with pytest.raises(HTTPException) as raised:
        auth_router.refresh(auth_router.RefreshRequest(refresh_token="already-rotated"))
    assert raised.value.status_code == 401

    assert seams.added_on("auth") == [], "a rejected refresh must not mint a new token"


# =====================================================================
# The module-level contract
# =====================================================================


def test_auth_router_holds_no_reference_to_the_app_role_session() -> None:
    """The strongest form of the fix: the confined seam is not in reach at all.

    `apps/api/app/routers/auth.py` imports only `get_auth_session`. If a
    future edit re-imports `get_session`, this fails and the author has
    to justify it against the module docstring — rather than discovering
    the consequence as an unexplained 401 in production.
    """
    import app.routers.auth as module

    assert not hasattr(module, "get_session"), (
        "app.routers.auth imported get_session again — every statement this module "
        "issues against refresh_tokens/users is pre-auth and belongs on the auth seam"
    )


def test_refresh_token_expiry_is_taken_from_settings(seams: _Seams) -> None:
    """Positive control: the issue path is really running, not short-circuited."""
    before = datetime.now(timezone.utc)
    auth_router.refresh(auth_router.RefreshRequest(refresh_token="presented-raw-token"))

    inserted = [o for o in seams.added_on("auth") if isinstance(o, RefreshToken)][0]
    assert inserted.expires_at >= before + timedelta(seconds=1209600 - 5)
