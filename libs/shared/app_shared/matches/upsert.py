"""Set-based match bulk-upsert core (`contracts/matches-bulk-upsert.md`, FR-013, SC-006).

Pure — compiles SQLAlchemy Core (``postgresql`` dialect) statements and
plain-data resolution maps; **never executes anything and never opens a
session**. ``apps/api/app/routers/matches.py`` executes the statement
this module builds inside the request's already-workspace-scoped
transaction. Reuses ``app_shared.catalog.upsert.dedup_last_wins``
unchanged (research D6).

## Single arbiter (bounded — SC-006)
A match has exactly one unique key: ``(workspace_id,
product_variant_id, competitor_id, normalized_competitor_url)``. So —
unlike the catalog upsert, which partitions by identity kind — the
whole safe batch is **one** ``INSERT ... ON CONFLICT DO UPDATE``,
regardless of ``len(rows)``. No per-row loop anywhere in this module.

## Router flow (`POST /v1/matches/bulk-upsert`, per the contract)
1. :func:`prepare_match_urls` splits the incoming rows into ``(safe,
   rejected)`` (FR-013 reject-and-report — an unsafe URL never aborts
   the rest of the batch).
2. :func:`app_shared.catalog.upsert.dedup_last_wins` collapses
   in-batch duplicates on :func:`match_conflict_key`.
3. :func:`variant_lookup_keys` + one scoped ``IN (...)`` variant select
   + :func:`resolve_match_variants` fill ``product_variant_id`` **and**
   ``product_id`` (from the variant's parent); unresolved rows are
   rejected by the router (workspace-consistency `422`).
4. The router consistency-checks ``competitor_id`` in-workspace (one
   scoped ``IN (...)`` lookup + ``assert_refs_in_workspace``).
5. :func:`build_matches_upsert` compiles the single set-based statement
   the router executes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.dialects.postgresql import Insert, insert as pg_insert
from sqlalchemy.sql import func

from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.url_pattern import derive_match_url_fields
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

# Re-exported so callers only need to import this module for the whole
# match bulk-upsert pipeline (`dedup_last_wins` itself is not
# reimplemented -- reused verbatim, research D6).
from app_shared.catalog.upsert import dedup_last_wins  # noqa: F401

# Columns updated by `ON CONFLICT ... DO UPDATE SET ...` -- deliberately
# EXCLUDES the four conflict columns (workspace_id,
# product_variant_id, competitor_id, normalized_competitor_url),
# product_id/workspace_id/id/created_at (immutable identity/audit
# columns), and every health field (health_status, last_error_code,
# consecutive_failures, success_rate_7d, current_price_id,
# last_scraped_at, last_success_at, last_failed_at) -- those are owned
# by SPEC-07+ and must never be reset by an idempotent re-push.
_MATCH_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "competitor_url",
    "url_pattern",
    "url_pattern_version",
    "competitor_variant_identifier",
    "competitor_variant_sku",
    "competitor_variant_options",
    "external_title",
    "scrape_profile_id",
    "access_policy_id",
    "priority",
    "status",
)

_CONFLICT_INDEX_ELEMENTS: list[str] = [
    "workspace_id",
    "product_variant_id",
    "competitor_id",
    "normalized_competitor_url",
]


def match_conflict_key(
    row: Mapping[str, Any],
) -> tuple[Any, Any, Any]:
    """The in-batch dedup + conflict key: ``(product_variant_id, competitor_id,
    normalized_competitor_url)`` (`workspace_id` is implicit -- a batch is
    always scoped to one caller workspace)."""
    return (
        row.get("product_variant_id"),
        row.get("competitor_id"),
        row.get("normalized_competitor_url"),
    )


def prepare_match_urls(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``rows`` into ``(safe, rejected)`` (FR-013 reject-and-report).

    For each row (by original index): run
    :func:`app_shared.url_safety.validate_competitor_url` on
    ``row["competitor_url"]`` then
    :func:`app_shared.url_pattern.derive_match_url_fields`. On
    :class:`~app_shared.url_safety.UnsafeUrlError`, append ``{"index",
    "code": "UNSAFE_URL", "reason", "url"}`` to ``rejected`` and drop the
    row -- the rest of the batch is never aborted. Otherwise stamp
    ``normalized_competitor_url``/``url_pattern``/``url_pattern_version``
    onto a copy of the row and append it to ``safe``.
    """
    safe: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        url = row.get("competitor_url")
        try:
            validate_competitor_url(url)
        except UnsafeUrlError as exc:
            rejected.append(
                {
                    "index": index,
                    "code": "UNSAFE_URL",
                    "reason": exc.reason.value,
                    "url": url,
                }
            )
            continue
        normalized_url, url_pattern, url_pattern_version = derive_match_url_fields(url)
        prepared = dict(row)
        prepared["normalized_competitor_url"] = normalized_url
        prepared["url_pattern"] = url_pattern
        prepared["url_pattern_version"] = url_pattern_version
        safe.append(prepared)
    return safe, rejected


def variant_lookup_keys(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[set[str], set[str], set[uuid.UUID]]:
    """Which variant identities need the router's one scoped ``IN (...)`` lookup.

    Returns ``(external_ids, skus, variant_ids)``. Every row names its
    variant by exactly one of ``product_variant_id`` /
    ``variant_external_id`` / ``variant_sku`` -- an explicit
    ``product_variant_id`` still needs a lookup (to resolve its parent
    ``product_id`` and confirm workspace membership), so it is included
    too (unlike the catalog's parent-resolution helper, which skips rows
    that already carry an explicit id).
    """
    external_ids: set[str] = set()
    skus: set[str] = set()
    variant_ids: set[uuid.UUID] = set()
    for row in rows:
        variant_id = row.get("product_variant_id")
        if variant_id is not None:
            variant_ids.add(variant_id)
        elif row.get("variant_external_id"):
            external_ids.add(row["variant_external_id"])
        elif row.get("variant_sku"):
            skus.add(row["variant_sku"])
    return external_ids, skus, variant_ids


def resolve_match_variants(
    rows: Sequence[Mapping[str, Any]],
    *,
    by_external_id: Mapping[str, tuple[uuid.UUID, uuid.UUID]],
    by_sku: Mapping[str, tuple[uuid.UUID, uuid.UUID]],
    by_id: Mapping[uuid.UUID, uuid.UUID],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fill each row's ``product_variant_id`` **and** ``product_id`` (parent).

    ``by_external_id``/``by_sku`` map a variant identity to ``(variant_id,
    product_id)``; ``by_id`` maps an explicit ``product_variant_id`` to
    its ``product_id``. These are the plain-data maps the router built
    from its single scoped lookup keyed by :func:`variant_lookup_keys`.
    Returns ``(resolved, unresolved)`` -- ``unresolved`` rows named a
    variant identity absent from those maps (or supplied none at all);
    the router rejects those via the workspace-consistency pre-check
    (`422`) instead of letting a composite-FK violation reach the DB as
    a raw ``IntegrityError``.
    """
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        variant_id = row.get("product_variant_id")
        if variant_id is not None:
            product_id = by_id.get(variant_id)
            if product_id is None:
                unresolved.append(row)
                continue
            row["product_id"] = product_id
            resolved.append(row)
            continue

        hit: tuple[uuid.UUID, uuid.UUID] | None = None
        external_id = row.get("variant_external_id")
        sku = row.get("variant_sku")
        if external_id:
            hit = by_external_id.get(external_id)
        elif sku:
            hit = by_sku.get(sku)

        if hit is None:
            unresolved.append(row)
            continue
        row["product_variant_id"], row["product_id"] = hit
        resolved.append(row)
    return resolved, unresolved


def build_matches_upsert(rows: Sequence[Mapping[str, Any]]) -> Insert:
    """One ``pg_insert(CompetitorProductMatch).values([...]).on_conflict_do_update(...)``.

    Infers the single 4-column unique arbiter
    (``uq_cpm_ws_variant_competitor_norm_url``) and updates every column
    in ``_MATCH_UPDATABLE_COLUMNS`` from ``excluded``, plus
    ``updated_at = now()``. Never updates the four conflict columns,
    ``product_id``/``workspace_id``/``id``/``created_at``, or any health
    field.
    """
    stmt = pg_insert(CompetitorProductMatch).values(list(rows))
    set_ = {col: stmt.excluded[col] for col in _MATCH_UPDATABLE_COLUMNS}
    set_["updated_at"] = func.now()
    return stmt.on_conflict_do_update(index_elements=_CONFLICT_INDEX_ELEMENTS, set_=set_)
