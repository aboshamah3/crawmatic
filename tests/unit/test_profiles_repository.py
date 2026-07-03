"""`app_shared/profiles/repository.py` unit tests (SPEC-06 US1 T019, FR-004/FR-021, SC-007).

Pure query-compilation assertions — no database. `assert_profile_assignable`
cases land with T031 (US2, Phase 4) once `assert_profile_assignable` is
added by T030; this file covers only the T018 read/manage helpers.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from app_shared.profiles.repository import (
    GLOBAL_DEFAULT_PROFILE_NAME,
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
