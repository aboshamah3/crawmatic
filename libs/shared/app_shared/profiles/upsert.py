"""Set-based tenant profile bulk-upsert core (`contracts/profiles-bulk-upsert.md`, FR-020, SC-008).

Pure — compiles SQLAlchemy Core (``postgresql`` dialect) statements;
**never executes anything and never opens a session**. Tenant-only:
every row carries the caller's ``workspace_id`` (never ``NULL``), so the
statement built here can never write a global row —
``apps/api/app/routers/scrape_profiles.py`` executes it inside the
request's already-workspace-scoped transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import Insert, insert as pg_insert
from sqlalchemy.sql import func

from app_shared.catalog.upsert import dedup_last_wins
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.profiles.validation import ProfileValidationError, validate_profile

__all__ = ["dedup_last_wins", "build_profiles_upsert", "prepare_profiles"]

# Every column updated by `ON CONFLICT ... DO UPDATE SET ...` — deliberately
# never `id`/`workspace_id`/`created_at` (immutable identity/audit
# columns). `updated_at` is refreshed separately via `func.now()` since a
# Core (non-ORM) upsert never fires the mapped column's Python-side
# `onupdate` callable.
_PROFILE_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "name",
    "mode",
    "adapter_key",
    "jsonld_enabled",
    "platform_patterns_enabled",
    "embedded_json_enabled",
    "price_selector",
    "price_xpath",
    "price_regex",
    "old_price_selector",
    "old_price_xpath",
    "old_price_regex",
    "currency_selector",
    "currency_xpath",
    "currency_regex",
    "stock_selector",
    "stock_xpath",
    "stock_regex",
    "title_selector",
    "title_xpath",
    "variant_strategy",
    "variant_selector_config",
    "price_transform_rules",
    "validation_rules",
    "confidence_rules",
    "wait_for_selector",
    "request_timeout_ms",
    "browser_timeout_ms",
    "headers",
    "cookies",
)


def build_profiles_upsert(rows: Sequence[Mapping[str, Any]]) -> Insert:
    """One ``pg_insert(ScrapeProfile).values([...]).on_conflict_do_update(...)``.

    Targets the tenant partial unique ``uq_scrape_profiles_workspace_id_name``
    exactly (``index_elements=["workspace_id", "name"]``,
    ``index_where=text("workspace_id IS NOT NULL")``) — this predicate
    must match the index's ``postgresql_where`` verbatim or Postgres
    can't infer the arbiter (SPEC-04 inference rule). Never writes a
    global row: every row here is expected to carry a non-``None``
    ``workspace_id`` (the caller's own).
    """
    stmt = pg_insert(ScrapeProfile).values(list(rows))
    set_ = {col: stmt.excluded[col] for col in _PROFILE_UPDATABLE_COLUMNS}
    set_["updated_at"] = func.now()
    return stmt.on_conflict_do_update(
        index_elements=["workspace_id", "name"],
        index_where=text("workspace_id IS NOT NULL"),
        set_=set_,
    )


def _profile_conflict_key(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return (row.get("workspace_id"), row.get("name"))


def prepare_profiles(
    rows: Sequence[Mapping[str, Any]], *, workspace_id: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate + dedup a batch (`contracts/profiles-bulk-upsert.md`, FR-020).

    Per row: run `validate_profile` (enums, regex compile+ReDoS, cookie
    deny, `validation_rules`, `confidence_rules`). A
    `ProfileValidationError` moves the row to ``rejected`` (with
    ``index``, ``name``, ``field``, ``code``, ``reason``); a valid row
    (with ``workspace_id`` stamped) moves to ``valid``. Never aborts the
    batch (reject-and-report, FR-020). Then
    `app_shared.catalog.upsert.dedup_last_wins` collapses same-key
    ``(workspace_id, name)`` rows, keeping the last.
    """
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            validate_profile(row)
        except ProfileValidationError as exc:
            rejected.append(
                {
                    "index": index,
                    "name": row.get("name"),
                    "field": exc.field,
                    "code": exc.code,
                    "reason": exc.message,
                }
            )
            continue
        stamped = dict(row)
        stamped["workspace_id"] = workspace_id
        valid.append(stamped)

    deduped = dedup_last_wins(valid, _profile_conflict_key)
    return list(deduped), rejected
