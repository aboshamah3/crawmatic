"""Webhook ORM model shape tests (SPEC-16 T012, FR-006/FR-016/FR-019).

Pure ORM/metadata assertions — no database. Verifies the two SPEC-16
tables (`webhook_endpoints`, `webhook_events`) match `data-model.md`
exactly: table names, single-column `id` PK + `created_at`/`updated_at`
(with `onupdate`) on `WebhookEndpoint`, the composite PK (incl. the
partition key) + `postgresql_partition_by` on `WebhookEvent`, both
exposing `workspace_id`, `event_types`/`payload` JSON-typed,
registration in `WORKSPACE_OWNED_MODELS` + re-export from
`app_shared.models`, and constraint/index name length (<=63 bytes).
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.dialects import postgresql

from app_shared.models import WebhookEndpoint, WebhookEvent
from app_shared.models.base import TimestampMixin, WorkspaceScopedBase
from app_shared.repository import WORKSPACE_OWNED_MODELS

_PG_DIALECT = postgresql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=_PG_DIALECT)


def _fk_constraints(table) -> dict[str, ForeignKeyConstraint]:
    return {
        fk.name: fk for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)
    }


def _all_constraint_and_index_names(table) -> list[str]:
    names = [c.name for c in table.constraints if c.name is not None]
    names.extend(ix.name for ix in table.indexes if ix.name is not None)
    return names


# --- WebhookEndpoint ---------------------------------------------------------


def test_webhook_endpoint_table_name_and_columns() -> None:
    table = WebhookEndpoint.__table__
    assert table.name == "webhook_endpoints"
    expected_columns = {
        "id",
        "workspace_id",
        "name",
        "url",
        "secret_encrypted",
        "secret_key_version",
        "enabled",
        "event_types",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_webhook_endpoint_uses_workspace_scoped_base_and_timestamp_mixin() -> None:
    assert WorkspaceScopedBase in WebhookEndpoint.__mro__
    assert TimestampMixin in WebhookEndpoint.__mro__
    assert WebhookEndpoint.__table__.c.workspace_id.nullable is False


def test_webhook_endpoint_single_column_pk() -> None:
    table = WebhookEndpoint.__table__
    assert list(table.primary_key.columns.keys()) == ["id"]


def test_webhook_endpoint_created_updated_at_present_with_onupdate() -> None:
    table = WebhookEndpoint.__table__
    assert table.c.created_at.nullable is False
    assert table.c.updated_at.nullable is False
    assert table.c.updated_at.onupdate is not None


def test_webhook_endpoint_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(WebhookEndpoint.__table__)
    assert "fk_webhook_endpoints_workspace_id_workspaces" in fks


def test_webhook_endpoint_nullability() -> None:
    table = WebhookEndpoint.__table__
    for col in ("name", "url", "enabled", "event_types"):
        assert table.c[col].nullable is False, col
    for col in ("secret_encrypted", "secret_key_version"):
        assert table.c[col].nullable is True, col


def test_webhook_endpoint_enabled_default_true() -> None:
    table = WebhookEndpoint.__table__
    assert table.c.enabled.default.arg is True


def test_webhook_endpoint_event_types_is_json_typed() -> None:
    table = WebhookEndpoint.__table__
    assert isinstance(table.c.event_types.type, postgresql.JSONB)
    assert table.c.event_types.nullable is False


# --- WebhookEvent (PARTITIONED) ----------------------------------------------


def test_webhook_event_table_name_and_columns() -> None:
    table = WebhookEvent.__table__
    assert table.name == "webhook_events"
    expected_columns = {
        "id",
        "created_at",
        "workspace_id",
        "event_type",
        "payload",
        "status",
        "delivered_at",
    }
    assert expected_columns.issubset(set(table.c.keys()))


def test_webhook_event_uses_workspace_scoped_base_no_timestamp_mixin() -> None:
    assert WorkspaceScopedBase in WebhookEvent.__mro__
    assert TimestampMixin not in WebhookEvent.__mro__
    assert WebhookEvent.__table__.c.workspace_id.nullable is False
    assert "updated_at" not in WebhookEvent.__table__.c.keys()


def test_webhook_event_composite_pk_includes_partition_key() -> None:
    table = WebhookEvent.__table__
    pk_columns = set(table.primary_key.columns.keys())
    assert pk_columns == {"id", "created_at"}
    assert table.c.created_at.primary_key is True


def test_webhook_event_is_partitioned_by_created_at() -> None:
    table = WebhookEvent.__table__
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (created_at)"


def test_webhook_event_workspace_id_fk_to_workspaces() -> None:
    fks = _fk_constraints(WebhookEvent.__table__)
    assert "fk_webhook_events_workspace_id_workspaces" in fks


def test_webhook_event_only_workspace_id_has_fk() -> None:
    # FR-019: soft references only — no FK on event_type/payload or any
    # other column besides workspace_id (the RLS anchor).
    fks = _fk_constraints(WebhookEvent.__table__)
    assert len(fks) == 1


def test_webhook_event_composite_indexes_present() -> None:
    table = WebhookEvent.__table__
    index_names = {ix.name for ix in table.indexes}
    assert "ix_webhook_events_ws_created_id" in index_names
    assert "ix_webhook_events_ws_type_created" in index_names

    by_name = {ix.name: ix for ix in table.indexes}
    assert list(by_name["ix_webhook_events_ws_created_id"].columns.keys()) == [
        "workspace_id",
        "created_at",
        "id",
    ]
    assert list(by_name["ix_webhook_events_ws_type_created"].columns.keys()) == [
        "workspace_id",
        "event_type",
        "created_at",
    ]


def test_webhook_event_nullability() -> None:
    table = WebhookEvent.__table__
    for col in ("event_type", "payload", "status"):
        assert table.c[col].nullable is False, col
    assert table.c.delivered_at.nullable is True


def test_webhook_event_status_default_pending() -> None:
    table = WebhookEvent.__table__
    assert table.c.status.default.arg == "PENDING"


def test_webhook_event_payload_is_json_typed() -> None:
    table = WebhookEvent.__table__
    assert isinstance(table.c.payload.type, postgresql.JSONB)


def test_webhook_event_event_type_and_status_render_varchar() -> None:
    table = WebhookEvent.__table__
    assert _compiled_type(table.c.event_type).upper() == "VARCHAR(64)"
    assert _compiled_type(table.c.status).upper() == "VARCHAR(32)"


# --- Registration + constraint/index name length -----------------------------


def test_both_models_registered_as_workspace_owned() -> None:
    for model in (WebhookEndpoint, WebhookEvent):
        assert model in WORKSPACE_OWNED_MODELS


def test_both_models_reexported_from_app_shared_models() -> None:
    import app_shared.models as models_pkg

    assert models_pkg.WebhookEndpoint is WebhookEndpoint
    assert models_pkg.WebhookEvent is WebhookEvent
    assert "WebhookEndpoint" in models_pkg.__all__
    assert "WebhookEvent" in models_pkg.__all__


def test_all_constraint_and_index_names_are_within_63_bytes() -> None:
    for table in (WebhookEndpoint.__table__, WebhookEvent.__table__):
        for name in _all_constraint_and_index_names(table):
            assert len(name.encode("utf-8")) <= 63, name
