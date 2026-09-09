"""In-Python evaluator for the set-based rollup batch statement (EPA C7).

Test support, not a test. `app_shared.maintenance.rollup_sql`'s statement
is one big ``WITH ... INSERT ... SELECT`` that only Postgres can execute,
but several DB-independent unit suites
(`test_rollup_watermark.py`, `test_backfill_daily_rollups.py`) need a
fake session that behaves like a database so that "idempotent", "no
double count" and "a crash before commit discards the batch" stay
*observed* properties rather than restatements of the code's comments.

This module re-implements the statement's semantics over in-memory rows,
driven by the statement's OWN bind parameters (``day_start``/``day_end``/
``last_workspace_id``/``last_product_variant_id``/``batch_limit``), so
the fake exercises the real predicate and the real keyset window rather
than a hand-written copy of them. It deliberately mirrors
:func:`app_shared.maintenance.rollups.aggregate_competitor_prices` for
the arithmetic — that function is the executable specification of the
same rule.

The authority on whether the *SQL* means this is
`tests/integration/test_rollup_set_based.py`, which runs it against a
real Postgres. This file only keeps the pure suites honest about the
driver loop around it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

__all__ = ["BatchOutcome", "evaluate_batch", "is_batch_statement"]

_MONEY_QUANT = Decimal("0.0001")


def is_batch_statement(sql: str) -> bool:
    """True for the rollup batch statement (write or dry-run shape)."""
    return sql.lstrip().startswith("WITH") and "price_observations" in sql


class BatchOutcome(SimpleNamespace):
    """The single row the real statement returns."""


def evaluate_batch(
    params: dict,
    *,
    dry_run: bool,
    observations: list,
    states: dict,
    upsert,
) -> BatchOutcome:
    """Evaluate one batch over in-memory rows.

    ``observations`` are objects with ``workspace_id``/
    ``product_variant_id``/``product_id``/``match_id``/``scraped_at``/
    ``price``/``currency``/``success``/``comparable``. ``states`` maps
    ``(workspace_id, product_variant_id)`` to an object with
    ``client_price``/``currency``/``latest_alert_type``. ``upsert`` is
    called once per written row with the values dict (never called when
    ``dry_run``).
    """
    day_start = params["day_start"]
    day_end = params["day_end"]
    cursor = (params["last_workspace_id"], params["last_product_variant_id"])
    limit = params["batch_limit"]

    in_day = [obs for obs in observations if day_start <= obs.scraped_at < day_end]

    # driver: DISTINCT ON (workspace_id, product_variant_id) beyond the
    # keyset cursor, ordered, LIMIT batch_limit.
    product_by_pair: dict[tuple, object] = {}
    for obs in in_day:
        pair = (obs.workspace_id, obs.product_variant_id)
        if pair > cursor:
            product_by_pair.setdefault(pair, obs.product_id)
    driver = sorted(product_by_pair)[:limit]
    driver_set = set(driver)

    skipped: list[str] = []
    written = 0
    for pair in driver:
        state = states.get(pair)
        if state is None:
            skipped.append(str(pair[1]))
            continue
        aggregate = _aggregate(
            [
                obs
                for obs in in_day
                if (obs.workspace_id, obs.product_variant_id) == pair
            ],
            client_currency=state.currency,
        )
        written += 1
        if dry_run:
            continue
        upsert(
            {
                "workspace_id": pair[0],
                "product_variant_id": pair[1],
                "product_id": product_by_pair[pair],
                "date": params["target_date"],
                "currency": state.currency,
                "client_price": state.client_price,
                "cheapest_competitor_price": aggregate[0],
                "average_competitor_price": aggregate[1],
                "highest_competitor_price": aggregate[2],
                "comparable_competitor_count": aggregate[3],
                "latest_alert_type": state.latest_alert_type,
            }
        )

    last_pair = max(driver_set) if driver_set else (None, None)
    return BatchOutcome(
        driver_rows=len(driver),
        rollups_upserted=written,
        variants_skipped_no_state=skipped or None,
        last_workspace_id=last_pair[0],
        last_product_variant_id=last_pair[1],
    )


def _aggregate(rows: list, *, client_currency: str):
    """MIN/ROUND(AVG,4)/MAX/COUNT over the latest ELIGIBLE row per match."""
    eligible = [
        row
        for row in rows
        if row.success
        and row.comparable
        and row.price is not None
        and row.currency == client_currency
    ]
    latest: dict = {}
    for index, row in enumerate(eligible):
        match_id = getattr(row, "match_id", None)
        key = match_id if match_id is not None else ("__unmatched__", index)
        incumbent = latest.get(key)
        if incumbent is None or row.scraped_at > incumbent.scraped_at:
            latest[key] = row
    prices = [Decimal(str(row.price)) for row in latest.values()]
    if not prices:
        return (None, None, None, 0)
    total = sum(prices, Decimal(0))
    average = (total / Decimal(len(prices))).quantize(
        _MONEY_QUANT, rounding=ROUND_HALF_UP
    )
    return (min(prices), average, max(prices), len(prices))
