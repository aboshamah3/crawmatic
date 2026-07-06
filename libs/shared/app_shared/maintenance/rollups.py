"""Daily rollup aggregation job (SPEC-15 US2, contracts/daily-rollup.md, research R6).

For UTC date ``D`` (default: yesterday UTC, the most-recently-COMPLETED
day — clarification), upserts one ``variant_price_daily_rollups`` row per
``(workspace_id, product_variant_id)`` that had >=1 ``price_observations``
row on ``D``:

* **Competitor min/avg/max + comparable count** are computed in Python
  from that day's raw observations (:func:`aggregate_competitor_prices`,
  a pure function — no live DB needed to unit-test it), filtered to
  ``success AND comparable AND price IS NOT NULL AND currency ==
  <client currency>`` (FR-011) — a currency mismatch (or a failed/
  unpriced/non-comparable observation) is excluded from BOTH the
  aggregate AND the count. ``comparable`` is the *persisted* SPEC-09
  decision (already false on a currency mismatch) — read, not
  recomputed (research R6).
* **``client_price``/``currency``/``latest_alert_type``/``product_id``**
  are read (not recomputed) from the SPEC-09 current-comparison surface
  ``variant_price_states``, scoped by ``workspace_id`` + ``product_variant_id``
  (R6) — the only source of client-price state (no per-day history exists).
* The upsert is keyed on ``(workspace_id, product_variant_id, date)``
  (``ON CONFLICT ... DO UPDATE``, FR-010) — idempotent re-run/backfill.

The driver scan (step 1: "which (workspace, variant) pairs had activity
on D") is inherently cross-tenant — one day spans every workspace — so
it runs unscoped on the BYPASSRLS system session (`# noqa:
workspace-scope`, research R9). Every subsequent read/write for a given
pair carries an explicit ``workspace_id=`` (FR-014).

Scraping-free (Constitution I/V) — SQLAlchemy + stdlib only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, NamedTuple

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app_shared.models.rollups import VariantPriceDailyRollup

# `variant_price_daily_rollups.average_competitor_price` is `NUMERIC(18,4)`
# (`Money`, FR-012) — an arithmetic mean of N observed prices is not
# generally exact at 4 decimal places (e.g. three prices averaged), and
# `Money` REJECTS an over-scale value rather than silently rounding it
# (`app_shared.money.parse_money`) — the right policy for a *comparison
# decision* boundary (`app_shared.alerts.engine`), where silently rounding
# could shift which side of a threshold a value falls on. This is a
# *stored snapshot* column, not a decision boundary, so the average is
# deliberately quantized to the column's own scale here (never silently
# rounding a value that matters to a decision — there is no decision
# here, only a fit-in-the-column requirement).
_MONEY_QUANT = Decimal("0.0001")


def _utc_today(now_utc: datetime) -> date_type:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (UTC)")
    return now_utc.astimezone(timezone.utc).date()


def default_target_date(now_utc: datetime) -> date_type:
    """Return the most-recently-COMPLETED UTC calendar day relative to ``now_utc``.

    The daily-rollup job's default cadence target (clarification,
    contracts/daily-rollup.md `Signature`) — "yesterday UTC" — so a day
    is only rolled up once it can no longer receive new observations
    from the current tick.
    """
    return _utc_today(now_utc) - timedelta(days=1)


class ObservationRow(NamedTuple):
    """One raw ``price_observations`` row's fields relevant to competitor
    aggregation — the minimal shape :func:`aggregate_competitor_prices`
    needs, independent of how the rows were fetched (a live query result
    or a fake row in a unit test)."""

    price: Decimal | None
    currency: str | None
    success: bool
    comparable: bool


@dataclass(frozen=True)
class CompetitorAggregate:
    """The four values written to ``variant_price_daily_rollups`` per
    (workspace, variant, day) for the competitor side (FR-011/012/013)."""

    cheapest: Decimal | None
    average: Decimal | None
    highest: Decimal | None
    comparable_count: int


def aggregate_competitor_prices(
    rows: Iterable[ObservationRow], *, client_currency: str
) -> CompetitorAggregate:
    """Pure aggregation over one (workspace, variant, day)'s raw observations.

    Filters to ``success AND comparable AND price IS NOT NULL AND
    currency == client_currency`` (FR-011) — a currency mismatch (already
    flagged ``comparable=False`` by SPEC-09) or any failed/unpriced/
    non-comparable row is excluded from BOTH the min/avg/max aggregate
    AND the count, never merely from the aggregate. Zero matching rows
    -> NULL ``cheapest``/``average``/``highest`` and ``comparable_count=0``
    (FR-013) — a valid result, never an error. All arithmetic is exact
    ``Decimal`` (never float); the average is quantized to the column's
    ``NUMERIC(18,4)`` scale with ``ROUND_HALF_UP`` (ties away from zero)
    since an arithmetic mean is not generally exact at 4 decimal places.
    """
    prices = [
        row.price
        for row in rows
        if row.success and row.comparable and row.price is not None and row.currency == client_currency
    ]
    if not prices:
        return CompetitorAggregate(cheapest=None, average=None, highest=None, comparable_count=0)

    total = sum(prices, Decimal(0))
    average = (total / Decimal(len(prices))).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    return CompetitorAggregate(
        cheapest=min(prices),
        average=average,
        highest=max(prices),
        comparable_count=len(prices),
    )


def _driver_pairs_stmt(target_date: date_type):
    """Build the (unexecuted) cross-tenant driver-scan statement.

    Distinct ``(workspace_id, product_variant_id, product_id)`` with >=1
    ``price_observations`` row on ``target_date`` (contracts/daily-rollup.md
    step 1) — inherently cross-tenant (one day spans every workspace), so
    this is the one unscoped scan in this module (`# noqa:
    workspace-scope`, research R9). Split out so its rendered SQL can be
    asserted in a pure unit test without a live DB (mirrors
    `app_shared.maintenance.partitions._to_regclass_stmt`).
    """
    return text(
        """
        SELECT DISTINCT workspace_id, product_variant_id, product_id
        FROM price_observations
        WHERE scraped_at::date = :target_date
        """
    ).bindparams(target_date=target_date)


def _day_observations_stmt(
    target_date: date_type, workspace_id: uuid.UUID, product_variant_id: uuid.UUID
):
    """Build the (unexecuted) statement fetching one (ws, variant, day)'s
    raw observation rows (price/currency/success/comparable) — the
    unfiltered input to :func:`aggregate_competitor_prices`. Explicitly
    scoped by ``workspace_id`` (FR-014) in addition to the day + variant.
    """
    return text(
        """
        SELECT price, currency, success, comparable
        FROM price_observations
        WHERE scraped_at::date = :target_date
          AND workspace_id = :workspace_id
          AND product_variant_id = :product_variant_id
        """
    ).bindparams(
        target_date=target_date,
        workspace_id=workspace_id,
        product_variant_id=product_variant_id,
    )


def _client_state_stmt(workspace_id: uuid.UUID, product_variant_id: uuid.UUID):
    """Build the (unexecuted) statement reading the SPEC-09 current-state
    surface (`client_price`/`currency`/`latest_alert_type`) for one
    variant — read, not recomputed (research R6). Explicitly scoped by
    ``workspace_id`` (FR-014).
    """
    return text(
        """
        SELECT client_price, currency, latest_alert_type
        FROM variant_price_states
        WHERE workspace_id = :workspace_id AND product_variant_id = :product_variant_id
        """
    ).bindparams(workspace_id=workspace_id, product_variant_id=product_variant_id)


@dataclass
class RunReport:
    """Structured summary of one ``run_daily_rollup`` run (FR-023,
    data-model.md §5) — logged by the Celery task wrapper, never
    persisted."""

    rollups_upserted: int = 0
    variants_skipped_no_state: list[str] = field(default_factory=list)


def run_daily_rollup(session: Session, *, target_date: date_type | None = None) -> RunReport:
    """Upsert one ``variant_price_daily_rollups`` row per (workspace,
    variant) that had >=1 observation on ``target_date`` (contracts/
    daily-rollup.md). ``target_date`` defaults to yesterday UTC
    (:func:`default_target_date`).

    A variant with observations that day but **no** SPEC-09
    ``variant_price_states`` row yet (never computed) has no
    ``client_price`` to snapshot (NOT NULL column) — recorded in
    ``variants_skipped_no_state`` and skipped rather than erroring or
    writing a placeholder; this is distinct from the *zero-comparable*
    case (FR-013), which still gets a row.
    """
    if target_date is None:
        target_date = default_target_date(datetime.now(timezone.utc))

    report = RunReport()

    # Cross-tenant scan (research R9) -- the one unscoped read in this
    # module; every subsequent read/write below carries an explicit
    # workspace_id= (FR-014).
    pairs = session.execute(_driver_pairs_stmt(target_date)).all()  # noqa: workspace-scope

    for row in pairs:
        workspace_id = row.workspace_id
        product_variant_id = row.product_variant_id
        product_id = row.product_id

        state_row = session.execute(
            _client_state_stmt(workspace_id, product_variant_id)
        ).first()
        if state_row is None:
            report.variants_skipped_no_state.append(str(product_variant_id))
            continue

        observation_rows = [
            ObservationRow(
                price=obs.price,
                currency=obs.currency,
                success=obs.success,
                comparable=obs.comparable,
            )
            for obs in session.execute(
                _day_observations_stmt(target_date, workspace_id, product_variant_id)
            )
        ]
        aggregate = aggregate_competitor_prices(
            observation_rows, client_currency=state_row.currency
        )

        values = {
            "workspace_id": workspace_id,
            "product_id": product_id,
            "product_variant_id": product_variant_id,
            "date": target_date,
            "currency": state_row.currency,
            "client_price": state_row.client_price,
            "cheapest_competitor_price": aggregate.cheapest,
            "average_competitor_price": aggregate.average,
            "highest_competitor_price": aggregate.highest,
            "comparable_competitor_count": aggregate.comparable_count,
            "latest_alert_type": state_row.latest_alert_type,
        }
        stmt = pg_insert(VariantPriceDailyRollup).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["workspace_id", "product_variant_id", "date"],
            set_={
                "product_id": stmt.excluded.product_id,
                "currency": stmt.excluded.currency,
                "client_price": stmt.excluded.client_price,
                "cheapest_competitor_price": stmt.excluded.cheapest_competitor_price,
                "average_competitor_price": stmt.excluded.average_competitor_price,
                "highest_competitor_price": stmt.excluded.highest_competitor_price,
                "comparable_competitor_count": stmt.excluded.comparable_competitor_count,
                "latest_alert_type": stmt.excluded.latest_alert_type,
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)
        report.rollups_upserted += 1

    return report
