"""ORM model shape tests for `ProxyProvider`/`AccessPolicy`/`DomainAccessRule`
(SPEC-10 T011, FR-001/FR-002/FR-004/FR-006).

Pure ORM/metadata assertions — no database. Verifies `proxy_providers`/
`access_policies`/`domain_access_rules` match `data-model.md` /
`contracts/models-access.md` exactly: table/column names + nullability,
every enum column rendering `VARCHAR` (not a DB-native enum), the
dual-scope tables carrying a nullable `workspace_id` + both partial
unique namespaces + `created_at`/`updated_at`, the tenant table carrying
a non-null `workspace_id` + the composite lookup index + the
`COALESCE(url_pattern, '')` uniqueness, the three soft references
(`provider_id`/`access_policy_id`/`competitor_id`) as plain `Uuid` with
no FK, correct `WORKSPACE_OWNED_MODELS` membership, re-export from
`app_shared.models`, and every constraint/index name <=63 bytes.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, Index
from sqlalchemy.dialects import postgresql

from app_shared.models.access import AccessPolicy, DomainAccessRule, ProxyProvider
from app_shared.models.base import Base, TimestampMixin, WorkspaceScopedBase

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)}


def _unique_indexes(table) -> dict[str, Index]:
    return {ix.name: ix for ix in table.indexes if ix.unique}


def _all_constraint_and_index_names(table) -> list[str]:
    names: list[str] = []
    for c in table.constraints:
        if getattr(c, "name", None):
            names.append(c.name)
    for ix in table.indexes:
        if ix.name:
            names.append(ix.name)
    return names


# --- ProxyProvider: dual-scope class shape ----------------------------------


def test_proxy_provider_uses_base_and_timestamp_mixin_not_workspace_scoped_base() -> None:
    assert Base in ProxyProvider.__mro__
    assert TimestampMixin in ProxyProvider.__mro__
    assert WorkspaceScopedBase not in ProxyProvider.__mro__


def test_proxy_provider_table_name_and_columns() -> None:
    table = ProxyProvider.__table__
    assert table.name == "proxy_providers"
    expected_columns = {
        "id",
        "workspace_id",
        "name",
        "type",
        "base_url",
        "username",
        "password_encrypted",
        "password_key_version",
        "country_code",
        "status",
        "monthly_budget_limit",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_proxy_provider_workspace_id_is_nullable_and_indexed() -> None:
    table = ProxyProvider.__table__
    assert table.c.workspace_id.nullable is True
    assert table.c.workspace_id.index is True


def test_proxy_provider_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(ProxyProvider.__table__)
    assert "fk_proxy_providers_workspace_id_workspaces" in fks
    fk = fks["fk_proxy_providers_workspace_id_workspaces"]
    assert [c.name for c in fk.columns] == ["workspace_id"]
    assert all(e.column.table.name == "workspaces" for e in fk.elements)


def test_proxy_provider_two_partial_unique_indexes_present() -> None:
    indexes = _unique_indexes(ProxyProvider.__table__)

    tenant = indexes["uq_proxy_providers_workspace_id_name"]
    assert set(tenant.columns.keys()) == {"workspace_id", "name"}
    assert str(tenant.dialect_options["postgresql"]["where"]) == "workspace_id IS NOT NULL"

    global_ = indexes["uq_proxy_providers_name_global"]
    assert set(global_.columns.keys()) == {"name"}
    assert str(global_.dialect_options["postgresql"]["where"]) == "workspace_id IS NULL"


def test_proxy_provider_password_columns_nullable_and_paired() -> None:
    table = ProxyProvider.__table__
    assert table.c.password_encrypted.nullable is True
    assert table.c.password_key_version.nullable is True


def test_proxy_provider_type_and_status_enum_columns_render_varchar() -> None:
    table = ProxyProvider.__table__
    assert "VARCHAR" in _compiled_type(table.c.type).upper()
    assert "VARCHAR" in _compiled_type(table.c.status).upper()
    assert table.c.type.nullable is False
    assert table.c.status.nullable is False


def test_proxy_provider_status_default_active() -> None:
    from app_shared.enums import ProxyProviderStatus

    assert ProxyProvider.__table__.c.status.default.arg == ProxyProviderStatus.ACTIVE


def test_proxy_provider_created_at_updated_at_not_null() -> None:
    table = ProxyProvider.__table__
    assert table.c.created_at.nullable is False
    assert table.c.updated_at.nullable is False


# --- AccessPolicy: dual-scope class shape -----------------------------------


def test_access_policy_uses_base_and_timestamp_mixin_not_workspace_scoped_base() -> None:
    assert Base in AccessPolicy.__mro__
    assert TimestampMixin in AccessPolicy.__mro__
    assert WorkspaceScopedBase not in AccessPolicy.__mro__


def test_access_policy_table_name_and_columns() -> None:
    table = AccessPolicy.__table__
    assert table.name == "access_policies"
    expected_columns = {
        "id",
        "workspace_id",
        "name",
        "strategy",
        "provider_id",
        "country_code",
        "use_proxy_on_first_attempt",
        "use_proxy_on_retry",
        "allow_browser_fallback",
        "max_retries",
        "rotate_per_request",
        "sticky_session",
        "session_ttl_minutes",
        "max_requests_per_minute",
        "max_requests_per_hour",
        "max_requests_per_day",
        "timeout_ms",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_access_policy_workspace_id_is_nullable_and_indexed() -> None:
    table = AccessPolicy.__table__
    assert table.c.workspace_id.nullable is True
    assert table.c.workspace_id.index is True


def test_access_policy_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(AccessPolicy.__table__)
    assert "fk_access_policies_workspace_id_workspaces" in fks


def test_access_policy_two_partial_unique_indexes_present() -> None:
    indexes = _unique_indexes(AccessPolicy.__table__)

    tenant = indexes["uq_access_policies_workspace_id_name"]
    assert set(tenant.columns.keys()) == {"workspace_id", "name"}
    assert str(tenant.dialect_options["postgresql"]["where"]) == "workspace_id IS NOT NULL"

    global_ = indexes["uq_access_policies_name_global"]
    assert set(global_.columns.keys()) == {"name"}
    assert str(global_.dialect_options["postgresql"]["where"]) == "workspace_id IS NULL"


def test_access_policy_provider_id_is_plain_uuid_with_no_fk() -> None:
    table = AccessPolicy.__table__
    assert table.c.provider_id.nullable is True
    fks = _fk_constraints(table)
    assert not any("provider_id" in [c.name for c in fk.columns] for fk in fks.values())


def test_access_policy_strategy_enum_column_renders_varchar() -> None:
    table = AccessPolicy.__table__
    assert "VARCHAR" in _compiled_type(table.c.strategy).upper()
    assert table.c.strategy.nullable is False


def test_access_policy_documented_defaults() -> None:
    table = AccessPolicy.__table__
    assert table.c.use_proxy_on_first_attempt.default.arg is False
    assert table.c.use_proxy_on_retry.default.arg is True
    assert table.c.allow_browser_fallback.default.arg is False
    assert table.c.max_retries.default.arg == 2
    assert table.c.rotate_per_request.default.arg is False
    assert table.c.sticky_session.default.arg is False
    assert table.c.timeout_ms.default.arg == 30000


def test_access_policy_nullable_ceiling_and_session_fields() -> None:
    table = AccessPolicy.__table__
    for field in (
        "session_ttl_minutes",
        "max_requests_per_minute",
        "max_requests_per_hour",
        "max_requests_per_day",
    ):
        assert table.c[field].nullable is True, field


# --- DomainAccessRule: tenant-only class shape ------------------------------


def test_domain_access_rule_uses_workspace_scoped_base() -> None:
    assert Base in DomainAccessRule.__mro__
    assert WorkspaceScopedBase in DomainAccessRule.__mro__
    assert TimestampMixin in DomainAccessRule.__mro__


def test_domain_access_rule_table_name_and_columns() -> None:
    table = DomainAccessRule.__table__
    assert table.name == "domain_access_rules"
    expected_columns = {
        "id",
        "workspace_id",
        "competitor_id",
        "domain",
        "url_pattern",
        "url_pattern_override",
        "access_policy_id",
        "max_concurrent_requests",
        "max_requests_per_minute",
        "cooldown_seconds",
        "block_detection_rules",
        "enabled",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_domain_access_rule_workspace_id_is_non_null() -> None:
    table = DomainAccessRule.__table__
    assert table.c.workspace_id.nullable is False
    assert table.c.workspace_id.index is True


def test_domain_access_rule_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(DomainAccessRule.__table__)
    assert "fk_domain_access_rules_workspace_id_workspaces" in fks


def test_domain_access_rule_competitor_id_and_access_policy_id_no_fk() -> None:
    table = DomainAccessRule.__table__
    assert table.c.competitor_id.nullable is False
    assert table.c.access_policy_id.nullable is False
    fks = _fk_constraints(table)
    referenced_columns = {c.name for fk in fks.values() for c in fk.columns}
    assert "competitor_id" not in referenced_columns
    assert "access_policy_id" not in referenced_columns


def test_domain_access_rule_composite_lookup_index_present() -> None:
    index_names = {ix.name: ix for ix in DomainAccessRule.__table__.indexes}
    name = "ix_domain_access_rules_workspace_id_competitor_id_domain"
    assert name in index_names
    assert list(index_names[name].columns.keys()) == [
        "workspace_id",
        "competitor_id",
        "domain",
    ]


def test_domain_access_rule_coalesce_uniqueness_index_present() -> None:
    indexes = _unique_indexes(DomainAccessRule.__table__)
    name = "uq_domain_access_rules_ws_cid_domain_pattern"
    assert name in indexes
    index = indexes[name]
    # The COALESCE expression is a non-column element; workspace_id/
    # competitor_id/domain remain named columns in the index.
    column_names = set(index.columns.keys())
    assert {"workspace_id", "competitor_id", "domain"}.issubset(column_names)


def test_domain_access_rule_enabled_default_true() -> None:
    assert DomainAccessRule.__table__.c.enabled.default.arg is True


def test_domain_access_rule_block_detection_rules_is_jsonb_nullable() -> None:
    table = DomainAccessRule.__table__
    assert "JSONB" in _compiled_type(table.c.block_detection_rules).upper()
    assert table.c.block_detection_rules.nullable is True


# --- WORKSPACE_OWNED_MODELS membership --------------------------------------


def test_proxy_provider_and_access_policy_absent_domain_access_rule_present() -> None:
    from app_shared.repository import WORKSPACE_OWNED_MODELS

    assert ProxyProvider not in WORKSPACE_OWNED_MODELS
    assert AccessPolicy not in WORKSPACE_OWNED_MODELS
    assert DomainAccessRule in WORKSPACE_OWNED_MODELS


# --- Re-export from app_shared.models ---------------------------------------


def test_models_reexported_from_app_shared_models() -> None:
    from app_shared.models import AccessPolicy as ReexportedAccessPolicy
    from app_shared.models import DomainAccessRule as ReexportedDomainAccessRule
    from app_shared.models import ProxyProvider as ReexportedProxyProvider

    assert ReexportedProxyProvider is ProxyProvider
    assert ReexportedAccessPolicy is AccessPolicy
    assert ReexportedDomainAccessRule is DomainAccessRule


# --- Name length budget (research pattern) ----------------------------------


def test_every_constraint_and_index_name_fits_63_bytes() -> None:
    for model in (ProxyProvider, AccessPolicy, DomainAccessRule):
        names = _all_constraint_and_index_names(model.__table__)
        assert names, f"{model.__name__} produced no constraint/index names"
        for name in names:
            assert len(name.encode("utf-8")) <= 63, f"{model.__name__}: {name}"
