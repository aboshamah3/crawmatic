"""Identity ORM model shape tests (SPEC-03 T039, FR-002/FR-003).

Pure ORM/metadata assertions — no database. Verifies the four identity
tables (`workspaces`, `users`, `refresh_tokens`, `api_keys`) match
data-model.md exactly on the properties the rest of this feature leans
on: `users.workspace_id` is nullable and NOT sourced from
`WorkspaceScopedBase` (whose column is NOT NULL); `api_keys.workspace_id`
is NOT NULL (via `WorkspaceScopedBase`); enum columns render as a plain
`VARCHAR` at the DDL level (never a Postgres-native enum) and coerce to
plain strings; `refresh_tokens` has `created_at` but no `updated_at`.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app_shared.enums import ApiKeyStatus, UserRole, UserStatus, WorkspaceStatus
from app_shared.models.base import WorkspaceScopedBase
from app_shared.models.identity import ApiKey, RefreshToken, User, Workspace

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


# --- Workspace: tenant root, no RLS -----------------------------------


def test_workspace_table_name_and_columns() -> None:
    table = Workspace.__table__
    assert table.name == "workspaces"
    expected_columns = {
        "id",
        "name",
        "slug",
        "status",
        "default_scrape_profile_id",
        "default_access_policy_id",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_workspace_slug_is_unique() -> None:
    table = Workspace.__table__
    assert table.c.slug.unique is True or any(
        "slug" in uq.columns.keys() for uq in table.constraints if hasattr(uq, "columns")
    )


def test_workspace_default_access_policy_id_is_nullable_with_no_fk() -> None:
    # default_access_policy_id has no FK yet — access_policies lands in a
    # later spec (SPEC-10).
    column = Workspace.__table__.c.default_access_policy_id
    assert column.nullable is True
    assert len(column.foreign_keys) == 0


def test_workspace_default_scrape_profile_id_is_nullable_fk_on_delete_set_null() -> None:
    # SPEC-06 promotes this column to a plain FK -> scrape_profiles(id)
    # ON DELETE SET NULL (research D5, FR-012/FR-023).
    column = Workspace.__table__.c.default_scrape_profile_id
    assert column.nullable is True
    assert len(column.foreign_keys) == 1
    fk = next(iter(column.foreign_keys))
    assert fk.column.table.name == "scrape_profiles"
    assert fk.ondelete == "SET NULL"


# --- User: workspace-owned (RLS), but OWN nullable workspace_id -------


def test_users_table_name_and_columns() -> None:
    table = User.__table__
    assert table.name == "users"
    expected_columns = {
        "id",
        "workspace_id",
        "email",
        "password_hash",
        "role",
        "status",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_users_workspace_id_is_nullable() -> None:
    assert User.__table__.c.workspace_id.nullable is True


def test_users_does_not_use_workspace_scoped_base() -> None:
    """User cannot inherit WorkspaceScopedBase — that mixin's column is NOT NULL."""
    assert WorkspaceScopedBase not in User.__mro__


def test_users_email_is_unique() -> None:
    table = User.__table__
    assert table.c.email.unique is True or any(
        "email" in uq.columns.keys() for uq in table.constraints if hasattr(uq, "columns")
    )


# --- ApiKey: workspace-owned (RLS), NOT NULL workspace_id -------------


def test_api_keys_table_name_and_columns() -> None:
    table = ApiKey.__table__
    assert table.name == "api_keys"
    expected_columns = {
        "id",
        "workspace_id",
        "name",
        "key_prefix",
        "key_hash",
        "scopes",
        "status",
        "last_used_at",
        "revoked_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_api_keys_workspace_id_is_not_null() -> None:
    assert ApiKey.__table__.c.workspace_id.nullable is False


def test_api_keys_uses_workspace_scoped_base() -> None:
    assert WorkspaceScopedBase in ApiKey.__mro__


def test_api_keys_key_prefix_is_indexed() -> None:
    table = ApiKey.__table__
    assert table.c.key_prefix.index is True or any(
        "key_prefix" in ix.columns.keys() for ix in table.indexes
    )


# --- RefreshToken: user-owned, no RLS, created_at only -----------------


def test_refresh_tokens_table_name_and_columns() -> None:
    table = RefreshToken.__table__
    assert table.name == "refresh_tokens"
    expected_columns = {
        "id",
        "user_id",
        "token_hash",
        "expires_at",
        "revoked_at",
        "created_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_refresh_tokens_has_no_updated_at() -> None:
    assert "updated_at" not in RefreshToken.__table__.c.keys()


def test_refresh_tokens_token_hash_is_unique() -> None:
    table = RefreshToken.__table__
    assert table.c.token_hash.unique is True or any(
        "token_hash" in uq.columns.keys() for uq in table.constraints if hasattr(uq, "columns")
    )


# --- Enum columns: plain VARCHAR at DDL level + string coercion --------


def test_enum_columns_render_as_plain_varchar() -> None:
    assert _compiled_type(Workspace.__table__.c.status).upper().startswith("VARCHAR")
    assert _compiled_type(User.__table__.c.role).upper().startswith("VARCHAR")
    assert _compiled_type(User.__table__.c.status).upper().startswith("VARCHAR")
    assert _compiled_type(ApiKey.__table__.c.status).upper().startswith("VARCHAR")


def test_enum_columns_coerce_to_plain_strings_on_bind() -> None:
    role_type = User.__table__.c.role.type
    bound = role_type.process_bind_param(UserRole.WORKSPACE_ADMIN, _PG_DIALECT)
    assert bound == "workspace_admin"
    assert isinstance(bound, str)
    assert type(bound) is str  # not a UserRole subclass leaking through to the DB driver


def test_enum_columns_coerce_from_db_strings_on_read() -> None:
    status_type = Workspace.__table__.c.status.type
    result = status_type.process_result_value("suspended", _PG_DIALECT)
    assert result == WorkspaceStatus.SUSPENDED
    assert str(result) == "suspended"


def test_api_key_status_enum_membership() -> None:
    status_type = ApiKey.__table__.c.status.type
    assert status_type.process_bind_param(ApiKeyStatus.REVOKED, _PG_DIALECT) == "revoked"


def test_user_status_enum_membership() -> None:
    status_type = User.__table__.c.status.type
    assert status_type.process_bind_param(UserStatus.ACTIVE, _PG_DIALECT) == "active"
