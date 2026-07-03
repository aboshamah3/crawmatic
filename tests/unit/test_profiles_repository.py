"""`app_shared/profiles/repository.py` unit tests (SPEC-06 US1 T019 + US2 T031,
FR-004/FR-013/FR-021, SC-002/SC-007).

Pure query-compilation assertions for the T018 read/manage helpers (no
database), plus `assert_profile_assignable` (T030) exercised over an
in-memory visibility map — `profile_visibility_map` is monkeypatched so
`assert_profile_assignable`'s ``session`` argument is never touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.catalog.consistency import CrossWorkspaceReference, MissingReference
from app_shared.profiles import repository as repository_module
from app_shared.profiles.repository import (
    GLOBAL_DEFAULT_PROFILE_NAME,
    assert_profile_assignable,
    owned_profile_select,
    visible_profiles_select,
)

_PG_DIALECT = postgresql.dialect()


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=_PG_DIALECT, compile_kwargs={"literal_binds": True}))


def test_global_default_profile_name_constant() -> None:
    assert GLOBAL_DEFAULT_PROFILE_NAME == "global_default"


def test_visible_profiles_select_emits_own_or_global_disjunct() -> None:
    ws = uuid.uuid4()
    sql = _compiled(visible_profiles_select(ws))

    assert "workspace_id" in sql
    assert str(ws) in sql
    assert "IS NULL" in sql
    assert " OR " in sql


def test_owned_profile_select_emits_workspace_id_only_no_global_disjunct() -> None:
    ws = uuid.uuid4()
    sql = _compiled(owned_profile_select(ws))

    assert "workspace_id" in sql
    assert str(ws) in sql
    assert "IS NULL" not in sql
    assert " OR " not in sql


# --- assert_profile_assignable (T030, FR-013/FR-017, SC-002) ---------------


class _FakeSession:
    """Never touched directly — `assert_profile_assignable` only calls
    `profile_visibility_map`, which is monkeypatched below to return an
    in-memory visibility map instead of issuing SQL."""


def _patch_visibility_map(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[uuid.UUID, uuid.UUID | None]
) -> None:
    def _fake_profile_visibility_map(
        session: Any, workspace_id: Any, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        assert isinstance(session, _FakeSession)
        return {id_: mapping[id_] for id_ in ids if id_ in mapping}

    monkeypatch.setattr(
        repository_module, "profile_visibility_map", _fake_profile_visibility_map
    )


def test_assert_profile_assignable_none_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = uuid.uuid4()
    _patch_visibility_map(monkeypatch, {})

    assert assert_profile_assignable(_FakeSession(), ws, None) is None


def test_assert_profile_assignable_own_workspace_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = uuid.uuid4()
    profile_id = uuid.uuid4()
    _patch_visibility_map(monkeypatch, {profile_id: ws})

    assert assert_profile_assignable(_FakeSession(), ws, profile_id) is None


def test_assert_profile_assignable_global_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = uuid.uuid4()
    profile_id = uuid.uuid4()
    _patch_visibility_map(monkeypatch, {profile_id: None})

    assert assert_profile_assignable(_FakeSession(), ws, profile_id) is None


def test_assert_profile_assignable_dangling_raises_missing_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = uuid.uuid4()
    profile_id = uuid.uuid4()
    _patch_visibility_map(monkeypatch, {})

    with pytest.raises(MissingReference):
        assert_profile_assignable(_FakeSession(), ws, profile_id)


def test_assert_profile_assignable_cross_workspace_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    profile_id = uuid.uuid4()
    _patch_visibility_map(monkeypatch, {profile_id: other_ws})

    with pytest.raises(CrossWorkspaceReference):
        assert_profile_assignable(_FakeSession(), ws, profile_id)
