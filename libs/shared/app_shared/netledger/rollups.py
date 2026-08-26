"""Durable, bounded-cardinality cost rollups over C1's network ledger (EPA C6).

## Why this module exists

``GET /ops/metrics`` (audit H5) needs aggregate cost health — total spend,
``estimated_vs_reconciled_variance_pct``, ledger freshness — and a
tenant-scoped surface needs its own per-domain/method/profile-version
breakdown. Neither may run a synchronous ``GROUP BY`` over
``network_operations``/``network_operation_allocations`` on every
request: those tables grow without bound and a request-time aggregation
over them is exactly the anti-pattern this task exists to forbid.

This module is the JOB that keeps
:class:`app_shared.models.network_cost_rollups.FleetNetworkCostRollup`
and :class:`~app_shared.models.network_cost_rollups.NetworkCostRollup`
current, on a durable cursor
(:mod:`app_shared.maintenance.rollup_watermark`, key
:data:`WATERMARK_COST_ROLLUP`) — reusing W5.5-L2's ``rollup_watermarks``
table with this run's own key namespace rather than inventing a second
watermark store, exactly as instructed. ``/ops/metrics`` and the tenant
cost-rollup endpoint (``apps/api/app/routers/cost_rollups.py``) read
ONLY the rollup tables this module writes, never the raw ledger.

## Split: pure aggregation, thin DB fetch

Mirrors ``app_shared.maintenance.rollups``' own
``aggregate_competitor_prices`` (pure) / ``_day_observations_stmt`` (raw
fetch) split, for the same reason: the CORRECTNESS-critical part (the
grouping, the top-N + "other" bounding, the fraction-of-settlement
arithmetic) is testable against plain fixture rows with no database at
all. :func:`aggregate_fleet_cost_buckets` and
:func:`aggregate_tenant_cost_buckets` take already-fetched rows and
return the bounded bucket set; :func:`run_cost_rollup` is the only
function that touches a session, and it does nothing but fetch three
day-scoped row sets, call the aggregators, and upsert the result.

## Bounded cardinality: top-N + "other", per currency

See ``app_shared.models.network_cost_rollups``' module docstring for the
full contract. Ranked by ``operation_count`` descending (ties broken by
the sort key, for determinism); the top
:data:`TOP_N_COST_ROLLUP_BUCKETS` survive as their own row, everything
else collapses into ONE additional row **per currency** using the
``COST_ROLLUP_OTHER_*``/``COST_ROLLUP_UNKNOWN_PROFILE_VERSION``
sentinels — bounding output at ``TOP_N_COST_ROLLUP_BUCKETS +
len(currencies)`` per scope+day, and the currency set is itself bounded
(ISO-4217 codes actually in use, never more than a handful in practice).
Bounding per-workspace for the tenant table, and fleet-wide for the
fleet table.

## "method" / "profile-version" — dimensions that actually separate

See ``app_shared.models.network_cost_rollups``' module docstring for the
full argument. In short: **method** is ``network_operations.transport``
(``DIRECT``/``PROXY``/``BROWSER`` — the split that differs by orders of
magnitude in cost), and **profile-version** is
``domain_playbooks.profile_version`` for the operation's domain (a real
playbook version an operator can compare two days across).

Neither is what C6 first shipped, and EPA Phase C's gate review was right
to reject those: the HTTP verb is constant (``GET`` on every scraping
fetch), so it separated nothing, and the budget decision version is a
per-decision counter tag, so it separated EVERYTHING — one bucket per
operation, top-N keeping twenty arbitrary ones and collapsing the rest
into ``__other__``. A dimension that is constant and a dimension that is
unique are the same bug: neither aggregates.

## Reconciled cost

A bucket's ``reconciled_cost_minor_units`` sums, over every operation in
the bucket, that operation's LATEST settlement
(``network_operation_settlements``, highest ``settlement_version`` —
never a mutable "current" column, matching C1/C5's own append-only
contract) — or ``None`` when not one operation in the bucket has a
settlement yet, which is a distinct fact from "reconciled to zero".
Tenant buckets apply this workspace's own ``fraction_ppb`` (parts per
billion, :data:`~app_shared.models.network_operations.FRACTION_SCALE`) to
each settlement before summing — an integer-floor split, not the
largest-remainder exactness :func:`~app_shared.models.network_operations.
allocate_cost_largest_remainder` guarantees for the authoritative
allocation row itself. That exactness is deliberately NOT reproduced
here: a rollup bucket is a reporting aggregate over potentially many
operations' settlements, not itself a per-operation ledger entry, so a
few minor units of floor-division drift across a whole day's bucket is
an acceptable, documented approximation — the authoritative figure
always remains ``network_operation_settlements`` itself.

Scraping-free (Constitution I/V) — SQLAlchemy + stdlib only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app_shared.maintenance.rollup_watermark import (
    advance_watermark,
    read_watermark,
    seed_watermark,
    watermark_store_available,
)
from app_shared.models.network_cost_rollups import (
    COST_ROLLUP_OTHER_DOMAIN,
    COST_ROLLUP_OTHER_METHOD,
    COST_ROLLUP_OTHER_PROFILE_VERSION,
    COST_ROLLUP_UNKNOWN_PROFILE_VERSION,
    FleetNetworkCostRollup,
    NetworkCostRollup,
)
from app_shared.models.network_operations import FRACTION_SCALE

logger = logging.getLogger(__name__)

__all__ = [
    "COST_ROLLUP_BACKFILL_MAX_DAYS",
    "COST_ROLLUP_SEED_LAG_DAYS",
    "TOP_N_COST_ROLLUP_BUCKETS",
    "WATERMARK_COST_ROLLUP",
    "CostBucketRow",
    "CostRollupReport",
    "RawAllocationRow",
    "RawOperationRow",
    "RawSettlementRow",
    "aggregate_fleet_cost_buckets",
    "aggregate_tenant_cost_buckets",
    "cost_rollup_day_bounds",
    "default_cost_rollup_target_date",
    "latest_settlements_by_operation",
    "run_cost_rollup",
]

#: Cursor key for this job's watermark, in the SAME ``rollup_watermarks``
#: table W5.5-L2 built (:mod:`app_shared.maintenance.rollup_watermark`) —
#: a distinct namespace from :data:`~app_shared.maintenance.
#: rollup_watermark.WATERMARK_DAILY_ROLLUP`, per this run's instruction
#: to reuse the pattern/table rather than invent a second store.
WATERMARK_COST_ROLLUP = "network_cost_rollup"

# --- Module-level constants awaiting a `Settings` home -----------------
# `libs/shared/app_shared/config.py` is held by a concurrent worker for
# the duration of this run (see this task's HARD FENCES) — these are
# module constants with the standard promotion marker rather than new
# `Settings` fields.

#: Buckets kept per (scope, day, currency) before the rest collapse into
#: one "other" row — see the module docstring's cardinality contract.
TOP_N_COST_ROLLUP_BUCKETS = 20  # TODO(config): promote to Settings
#: How many owed days a cadence-mode call (no explicit `target_date`)
#: will catch up in one invocation, oldest first — mirrors
#: `app_shared.maintenance.rollups`' `ROLLUP_BACKFILL_MAX_DAYS` shape.
COST_ROLLUP_BACKFILL_MAX_DAYS = 7  # TODO(config): promote to Settings
#: First-use watermark seed lag (see `seed_watermark`'s docstring for why
#: not the epoch): the cursor is born at `latest_complete_day - LAG`, so
#: the very first run does one day, not a walk to 1970.
COST_ROLLUP_SEED_LAG_DAYS = 1  # TODO(config): promote to Settings


def _utc_today(now_utc: datetime) -> date_type:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (UTC)")
    return now_utc.astimezone(timezone.utc).date()


def default_cost_rollup_target_date(now_utc: datetime) -> date_type:
    """Yesterday UTC — the most-recently-COMPLETED day (mirrors
    ``app_shared.maintenance.rollups.default_target_date``)."""
    return _utc_today(now_utc) - timedelta(days=1)


def cost_rollup_day_bounds(target_date: date_type) -> tuple[datetime, datetime]:
    """Half-open UTC instant range ``[D 00:00:00+00, D+1 00:00:00+00)``.

    Same sargability/timezone-determinism reasoning as
    ``app_shared.maintenance.rollups.utc_day_bounds`` — every predicate
    below is expressed against these bounds rather than
    ``closed_at::date = D``, so ``ix_network_operations_provider_
    created_at``-adjacent scans stay index-friendly and the meaning of
    "day D" never silently follows a session ``TimeZone``.
    """
    start = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------------------
# Raw row shapes (what the DB fetch returns; also the pure-aggregator input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawOperationRow:
    """One ``network_operations`` row closed on the target day, the
    columns this rollup groups/sums by."""

    network_request_id: uuid.UUID
    domain: str
    #: ``network_operations.transport`` as text (``DIRECT``/``PROXY``/
    #: ``BROWSER``) — the rollup's "method" dimension.
    method: str
    #: ``domain_playbooks.profile_version`` for this operation's domain,
    #: as text. ``None`` (no playbook row for the domain) maps to
    #: :data:`~app_shared.models.network_cost_rollups.
    #: COST_ROLLUP_UNKNOWN_PROFILE_VERSION` by the aggregator, never left
    #: as SQL NULL in a bucket key.
    profile_version: str | None
    estimated_cost_minor_units: int
    currency: str


@dataclass(frozen=True)
class RawAllocationRow:
    """One ``network_operation_allocations`` row for an operation closed
    on the target day."""

    operation_id: uuid.UUID
    workspace_id: uuid.UUID
    fraction_ppb: int
    allocated_cost_minor_units: int
    currency: str


@dataclass(frozen=True)
class RawSettlementRow:
    """One ``network_operation_settlements`` row for an operation closed
    on the target day (every version — the aggregator reduces to
    latest)."""

    operation_id: uuid.UUID
    settlement_version: int
    reconciled_cost_minor_units: int
    currency: str


@dataclass(frozen=True)
class CostBucketRow:
    """One bounded output row — a fleet row (``workspace_id is None``) or
    a tenant row."""

    workspace_id: uuid.UUID | None
    domain: str
    method: str
    profile_version: str
    currency: str
    operation_count: int
    estimated_cost_minor_units: int
    #: ``None`` iff not one operation in the bucket has a settlement yet.
    reconciled_cost_minor_units: int | None


def latest_settlements_by_operation(
    settlements: Iterable[RawSettlementRow],
) -> dict[uuid.UUID, RawSettlementRow]:
    """Reduce possibly-many settlement versions per operation to the
    single latest (highest ``settlement_version``) — the append-only
    ledger's own "current reconciled cost" definition
    (``NetworkOperationSettlement``'s docstring), reproduced here in
    Python rather than re-derived per bucket.
    """
    latest: dict[uuid.UUID, RawSettlementRow] = {}
    for row in settlements:
        current = latest.get(row.operation_id)
        if current is None or row.settlement_version > current.settlement_version:
            latest[row.operation_id] = row
    return latest


def _bucket_key(op: RawOperationRow) -> tuple[str, str, str, str]:
    profile_version = op.profile_version or COST_ROLLUP_UNKNOWN_PROFILE_VERSION
    return (op.domain, op.method, profile_version, op.currency)


@dataclass
class _MutableBucket:
    operation_count: int = 0
    estimated_cost_minor_units: int = 0
    reconciled_cost_minor_units: int = 0
    has_reconciled: bool = False


def _collapse_to_top_n(
    buckets: dict[tuple[str, str, str, str], _MutableBucket],
    *,
    top_n: int,
) -> dict[tuple[str, str, str, str], _MutableBucket]:
    """Rank ``buckets`` by ``operation_count`` desc (key as tiebreak for
    determinism), keep the top ``top_n``, collapse the rest into one
    "other" bucket per currency — see the module docstring's cardinality
    contract.
    """
    if len(buckets) <= top_n:
        return buckets

    ranked = sorted(buckets.items(), key=lambda kv: (-kv[1].operation_count, kv[0]))
    kept = dict(ranked[:top_n])
    overflow = ranked[top_n:]

    other_by_currency: dict[str, _MutableBucket] = {}
    for (_, _, _, currency), bucket in overflow:
        other = other_by_currency.setdefault(currency, _MutableBucket())
        other.operation_count += bucket.operation_count
        other.estimated_cost_minor_units += bucket.estimated_cost_minor_units
        if bucket.has_reconciled:
            other.has_reconciled = True
            other.reconciled_cost_minor_units += bucket.reconciled_cost_minor_units

    for currency, other in other_by_currency.items():
        key = (
            COST_ROLLUP_OTHER_DOMAIN,
            COST_ROLLUP_OTHER_METHOD,
            COST_ROLLUP_OTHER_PROFILE_VERSION,
            currency,
        )
        existing = kept.get(key)
        if existing is None:
            kept[key] = other
        else:
            existing.operation_count += other.operation_count
            existing.estimated_cost_minor_units += other.estimated_cost_minor_units
            if other.has_reconciled:
                existing.has_reconciled = True
                existing.reconciled_cost_minor_units += other.reconciled_cost_minor_units
    return kept


def aggregate_fleet_cost_buckets(
    operations: Sequence[RawOperationRow],
    settlements: Sequence[RawSettlementRow],
    *,
    top_n: int = TOP_N_COST_ROLLUP_BUCKETS,
) -> tuple[CostBucketRow, ...]:
    """Fleet-wide bounded buckets for one day.

    Pure — no session, no clock. Testable against plain fixture rows
    (the "rollup correctness vs raw fixture data" requirement).
    """
    latest = latest_settlements_by_operation(settlements)
    buckets: dict[tuple[str, str, str, str], _MutableBucket] = {}

    for op in operations:
        key = _bucket_key(op)
        bucket = buckets.setdefault(key, _MutableBucket())
        bucket.operation_count += 1
        bucket.estimated_cost_minor_units += op.estimated_cost_minor_units

        settlement = latest.get(op.network_request_id)
        if settlement is not None and settlement.currency == op.currency:
            bucket.has_reconciled = True
            bucket.reconciled_cost_minor_units += settlement.reconciled_cost_minor_units

    bounded = _collapse_to_top_n(buckets, top_n=top_n)
    return tuple(
        CostBucketRow(
            workspace_id=None,
            domain=domain,
            method=method,
            profile_version=profile_version,
            currency=currency,
            operation_count=b.operation_count,
            estimated_cost_minor_units=b.estimated_cost_minor_units,
            reconciled_cost_minor_units=(
                b.reconciled_cost_minor_units if b.has_reconciled else None
            ),
        )
        for (domain, method, profile_version, currency), b in sorted(bounded.items())
    )


def aggregate_tenant_cost_buckets(
    operations: Sequence[RawOperationRow],
    allocations: Sequence[RawAllocationRow],
    settlements: Sequence[RawSettlementRow],
    *,
    top_n: int = TOP_N_COST_ROLLUP_BUCKETS,
) -> tuple[CostBucketRow, ...]:
    """Per-workspace bounded buckets for one day (top-N applied
    independently WITHIN each workspace's own bucket set, never across
    workspaces — a whale tenant must never crowd a small tenant's own
    top-N out of existence).

    Pure — no session, no clock.
    """
    op_by_id = {op.network_request_id: op for op in operations}
    latest = latest_settlements_by_operation(settlements)

    per_workspace: dict[uuid.UUID, dict[tuple[str, str, str, str], _MutableBucket]] = {}

    for alloc in allocations:
        op = op_by_id.get(alloc.operation_id)
        if op is None:
            # An allocation for an operation outside this day's fetch --
            # cannot happen given the join the caller uses, but skipped
            # rather than raised: a rollup must degrade, never abort a
            # whole day for one inconsistent row.
            continue
        key = _bucket_key(op)
        ws_buckets = per_workspace.setdefault(alloc.workspace_id, {})
        bucket = ws_buckets.setdefault(key, _MutableBucket())
        bucket.operation_count += 1
        bucket.estimated_cost_minor_units += alloc.allocated_cost_minor_units

        settlement = latest.get(alloc.operation_id)
        if settlement is not None and settlement.currency == alloc.currency:
            share = (alloc.fraction_ppb * settlement.reconciled_cost_minor_units) // FRACTION_SCALE
            bucket.has_reconciled = True
            bucket.reconciled_cost_minor_units += share

    out: list[CostBucketRow] = []
    for workspace_id, ws_buckets in per_workspace.items():
        bounded = _collapse_to_top_n(ws_buckets, top_n=top_n)
        for (domain, method, profile_version, currency), b in sorted(bounded.items()):
            out.append(
                CostBucketRow(
                    workspace_id=workspace_id,
                    domain=domain,
                    method=method,
                    profile_version=profile_version,
                    currency=currency,
                    operation_count=b.operation_count,
                    estimated_cost_minor_units=b.estimated_cost_minor_units,
                    reconciled_cost_minor_units=(
                        b.reconciled_cost_minor_units if b.has_reconciled else None
                    ),
                )
            )
    out.sort(
        key=lambda r: (
            str(r.workspace_id), r.domain, r.method, r.profile_version, r.currency
        )
    )
    return tuple(out)


# ---------------------------------------------------------------------------
# DB fetch (raw SQL, unscoped cross-tenant scans -- same sanctioned seam
# as C1/C5's own cross-tenant reads over this same ledger)
# ---------------------------------------------------------------------------


def _operations_stmt(day_start: datetime, day_end: datetime):
    """The day's closed, priced operations with their two real dimensions.

    ``transport`` is on the row. ``profile_version`` is not — the ledger
    records no playbook version — so it comes from a LEFT JOIN on
    ``domain_playbooks`` by domain, which is the simplest honest source
    available (see this module's docstring for what that approximates and
    why the alternative was worse). LEFT, so an operation on a domain
    with no playbook still appears, with a NULL the aggregator maps to
    the UNKNOWN sentinel — a rollup that silently dropped uncertified
    domains' spend would hide exactly the spend an operator most wants to
    see.
    """
    return text(
        """
        SELECT no.network_request_id,
               no.domain,
               no.transport AS method,
               dp.profile_version::text AS profile_version,
               no.estimated_cost_minor_units,
               no.currency
        FROM network_operations no
        LEFT JOIN domain_playbooks dp ON dp.domain = no.domain
        WHERE no.closed_at IS NOT NULL
          AND no.closed_at >= :day_start AND no.closed_at < :day_end
          AND no.estimated_cost_minor_units IS NOT NULL
          AND no.currency IS NOT NULL
        """
    ).bindparams(day_start=day_start, day_end=day_end)


def _allocations_stmt(day_start: datetime, day_end: datetime):
    return text(
        """
        SELECT noa.operation_id, noa.workspace_id, noa.fraction_ppb,
               noa.allocated_cost_minor_units, noa.currency
        FROM network_operation_allocations noa
        JOIN network_operations no ON no.network_request_id = noa.operation_id
        WHERE no.closed_at IS NOT NULL
          AND no.closed_at >= :day_start AND no.closed_at < :day_end
        """
    ).bindparams(day_start=day_start, day_end=day_end)


def _settlements_stmt(day_start: datetime, day_end: datetime):
    return text(
        """
        SELECT s.operation_id, s.settlement_version, s.reconciled_cost_minor_units,
               s.currency
        FROM network_operation_settlements s
        JOIN network_operations no ON no.network_request_id = s.operation_id
        WHERE no.closed_at IS NOT NULL
          AND no.closed_at >= :day_start AND no.closed_at < :day_end
        """
    ).bindparams(day_start=day_start, day_end=day_end)


def _fetch_day_rows(
    session: Session, target_date: date_type
) -> tuple[list[RawOperationRow], list[RawAllocationRow], list[RawSettlementRow]]:
    day_start, day_end = cost_rollup_day_bounds(target_date)

    # Cross-tenant scans -- the same sanctioned seam C1/C5 use over this
    # same ledger (network_operations/network_operation_settlements carry
    # no workspace_id at all; network_operation_allocations is read here
    # joined THROUGH the fleet-owned parent, not filtered by any
    # workspace context).
    operations = [
        RawOperationRow(
            network_request_id=row.network_request_id,
            domain=row.domain,
            method=str(row.method),
            profile_version=row.profile_version,
            estimated_cost_minor_units=int(row.estimated_cost_minor_units),
            currency=row.currency,
        )
        for row in session.execute(_operations_stmt(day_start, day_end))  # noqa: workspace-scope
    ]
    allocations = [
        RawAllocationRow(
            operation_id=row.operation_id,
            workspace_id=row.workspace_id,
            fraction_ppb=int(row.fraction_ppb),
            allocated_cost_minor_units=int(row.allocated_cost_minor_units),
            currency=row.currency,
        )
        for row in session.execute(_allocations_stmt(day_start, day_end))  # noqa: workspace-scope
    ]
    settlements = [
        RawSettlementRow(
            operation_id=row.operation_id,
            settlement_version=int(row.settlement_version),
            reconciled_cost_minor_units=int(row.reconciled_cost_minor_units),
            currency=row.currency,
        )
        for row in session.execute(_settlements_stmt(day_start, day_end))  # noqa: workspace-scope
    ]
    return operations, allocations, settlements


def _upsert_fleet_buckets(session: Session, target_date: date_type, buckets: Sequence[CostBucketRow]) -> int:
    written = 0
    for bucket in buckets:
        stmt = pg_insert(FleetNetworkCostRollup).values(
            rollup_date=target_date,
            domain=bucket.domain,
            method=bucket.method,
            profile_version=bucket.profile_version,
            operation_count=bucket.operation_count,
            estimated_cost_minor_units=bucket.estimated_cost_minor_units,
            reconciled_cost_minor_units=bucket.reconciled_cost_minor_units,
            currency=bucket.currency,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "rollup_date", "domain", "method", "profile_version", "currency"
            ],
            set_={
                "operation_count": stmt.excluded.operation_count,
                "estimated_cost_minor_units": stmt.excluded.estimated_cost_minor_units,
                "reconciled_cost_minor_units": stmt.excluded.reconciled_cost_minor_units,
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)
        written += 1
    return written


def _upsert_tenant_buckets(session: Session, target_date: date_type, buckets: Sequence[CostBucketRow]) -> int:
    written = 0
    for bucket in buckets:
        stmt = pg_insert(NetworkCostRollup).values(
            workspace_id=bucket.workspace_id,
            rollup_date=target_date,
            domain=bucket.domain,
            method=bucket.method,
            profile_version=bucket.profile_version,
            operation_count=bucket.operation_count,
            estimated_cost_minor_units=bucket.estimated_cost_minor_units,
            reconciled_cost_minor_units=bucket.reconciled_cost_minor_units,
            currency=bucket.currency,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "workspace_id",
                "rollup_date",
                "domain",
                "method",
                "profile_version",
            ],
            set_={
                "operation_count": stmt.excluded.operation_count,
                "estimated_cost_minor_units": stmt.excluded.estimated_cost_minor_units,
                "reconciled_cost_minor_units": stmt.excluded.reconciled_cost_minor_units,
                "currency": stmt.excluded.currency,
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)
        written += 1
    return written


@dataclass
class CostRollupReport:
    """Structured summary of one :func:`run_cost_rollup` call — logged by
    the Celery task wrapper, never persisted (mirrors ``app_shared.
    maintenance.rollups.RunReport``)."""

    days_processed: list[date_type] = field(default_factory=list)
    fleet_rows_upserted: int = 0
    tenant_rows_upserted: int = 0
    watermark_advanced: bool = False
    watermark_store_available: bool = False


def _run_one_day(session: Session, target_date: date_type, *, top_n: int) -> tuple[int, int]:
    operations, allocations, settlements = _fetch_day_rows(session, target_date)
    fleet_buckets = aggregate_fleet_cost_buckets(operations, settlements, top_n=top_n)
    tenant_buckets = aggregate_tenant_cost_buckets(
        operations, allocations, settlements, top_n=top_n
    )
    fleet_written = _upsert_fleet_buckets(session, target_date, fleet_buckets)
    tenant_written = _upsert_tenant_buckets(session, target_date, tenant_buckets)
    return fleet_written, tenant_written


def run_cost_rollup(
    session: Session,
    *,
    target_date: date_type | None = None,
    now: datetime | None = None,
    top_n: int = TOP_N_COST_ROLLUP_BUCKETS,
    backfill_max_days: int = COST_ROLLUP_BACKFILL_MAX_DAYS,
    seed_lag_days: int = COST_ROLLUP_SEED_LAG_DAYS,
    commit: bool = True,
) -> CostRollupReport:
    """Upsert bounded fleet + tenant cost-rollup rows.

    * **One explicit day** (``target_date`` supplied) — rolls up exactly
      that day and does NOT touch the watermark. Mirrors ``app_shared.
      maintenance.rollups.run_daily_rollup``'s explicit-day mode: a
      manual/backfill call that must never move the cadence cursor
      underneath a concurrently-running scheduled pass.
    * **Cadence (``target_date=None``)** — durable-watermark catch-up:
      every UTC day owed since the ``rollup_watermarks`` cursor (key
      :data:`WATERMARK_COST_ROLLUP`), oldest first, bounded to
      ``backfill_max_days`` per call. Each day's upserts + watermark
      advance are committed TOGETHER (``commit=True``, the default)
      before the next day starts — mirrors ``app_shared.maintenance.
      rollups.run_rollup_catchup``'s own ``commit`` parameter and its
      no-loss/no-double-count ordering contract: a crash mid catch-up
      loses at most the in-flight day, never re-derives or re-advances a
      day already durable (idempotent upserts make even a torn-looking
      retry converge). When the watermark store is not migrated yet
      (``watermark_store_available`` is ``False``), degrades to exactly
      one day — :func:`default_cost_rollup_target_date` — with a single
      WARNING, the same degradation ``run_daily_rollup``'s cadence path
      uses.

    ``commit=False`` (tests only) inspects one transaction's statements
    without a live COMMIT; explicit-day mode never commits regardless —
    the caller owns that transaction, same as ``run_daily_rollup``.
    """
    now = now or datetime.now(timezone.utc)
    report = CostRollupReport()

    if target_date is not None:
        fleet_written, tenant_written = _run_one_day(session, target_date, top_n=top_n)
        report.days_processed.append(target_date)
        report.fleet_rows_upserted += fleet_written
        report.tenant_rows_upserted += tenant_written
        return report

    store_available = watermark_store_available(session)
    report.watermark_store_available = store_available

    if not store_available:
        logger.warning(
            "network_cost_rollup_watermark_store_absent -- degrading to "
            "single-day (yesterday UTC) rollup"
        )
        one_day = default_cost_rollup_target_date(now)
        fleet_written, tenant_written = _run_one_day(session, one_day, top_n=top_n)
        if commit:
            session.commit()
        report.days_processed.append(one_day)
        report.fleet_rows_upserted += fleet_written
        report.tenant_rows_upserted += tenant_written
        return report

    latest_complete_day = default_cost_rollup_target_date(now)
    watermark = read_watermark(session, key=WATERMARK_COST_ROLLUP)
    if watermark is None:
        seed_date = latest_complete_day - timedelta(days=seed_lag_days)
        seed_watermark(session, seed_date, key=WATERMARK_COST_ROLLUP, now=now)
        if commit:
            session.commit()
        watermark_date = seed_date
    else:
        watermark_date = watermark.last_complete_date

    day = watermark_date + timedelta(days=1)
    processed = 0
    while day <= latest_complete_day and processed < backfill_max_days:
        fleet_written, tenant_written = _run_one_day(session, day, top_n=top_n)
        # Same transaction as the day's own upserts -- the ordering
        # contract in `app_shared.maintenance.rollup_watermark`.
        advance_watermark(session, day, key=WATERMARK_COST_ROLLUP, now=now)
        if commit:
            session.commit()
        report.days_processed.append(day)
        report.fleet_rows_upserted += fleet_written
        report.tenant_rows_upserted += tenant_written
        report.watermark_advanced = True
        processed += 1
        day += timedelta(days=1)

    return report
