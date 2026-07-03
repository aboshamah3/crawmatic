"""`price_analysis` queue task: `recompute_variant` (SPEC-09 US1 T017,
US2 T023, contracts/price-analysis-task.md).

Thin orchestrator over the pure `app_shared.alerts.engine` — reads one
variant's client price/currency + its comparable `match_current_prices`,
runs the engine, and idempotently upserts `variant_price_states` +
`variant_alert_states`, writing a `price_alert_events` row **only** on a
type/severity change (step 9) and linking
`variant_price_states.latest_alert_state_id` to the upserted alert-state
row in the same transaction. Relies on the existing
`worker_process_init` -> `dispose_engine` fork-safety hook
(`celery_app.py`, SPEC-01/08) — never starts Scrapy in-process
(Principle V). Runs on its own `price_analysis` queue, separate from
`scrape_dispatch`/`maintenance` (D4, §26).

Idempotent (FR-014, SC-001, SC-007): re-running with unchanged inputs
yields an equal engine `AlertOutcome`, so both upserts write identical
state (only `calculated_at`/`updated_at`/`last_seen_at` advance) and
`transition()` returns `None` (no duplicate event). The emission-side
Redis `SET NX` dedup (D4/D7, US3) is a contention reducer, not a
correctness guard — at-least-once Celery delivery is always safe here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.workers.celery_app import app
from app_shared.alerts import engine
from app_shared.alerts.engine import AlertOutcome, CompetitorPrice
from app_shared.database import get_session, set_workspace_context
from app_shared.enums import (
    AlertEventType,
    AlertSeverity,
    AlertStatus,
    AlertType,
    ScrapeErrorCode,
)
from app_shared.models.alerts import PriceAlertEvent, VariantAlertState, VariantPriceState
from app_shared.models.catalog import ProductVariant
from app_shared.models.observations import MatchCurrentPrice
from app_shared.repository import scoped_get, scoped_select
from app_shared.task_names import PRICE_ANALYSIS_RECOMPUTE


def _load_competitor_rows(
    session: Session, workspace_id: uuid.UUID, variant_id: uuid.UUID
) -> list[CompetitorPrice]:
    rows = (
        session.execute(
            scoped_select(MatchCurrentPrice, workspace_id).where(
                MatchCurrentPrice.product_variant_id == variant_id
            )
        )
        .scalars()
        .all()
    )
    return [
        CompetitorPrice(
            match_id=row.match_id,
            price=row.price,
            currency=row.currency,
            success=row.success,
            comparable=row.comparable,
        )
        for row in rows
    ]


def _write_back_currency_mismatches(
    session: Session,
    workspace_id: uuid.UUID,
    mismatched_match_ids: list[uuid.UUID],
) -> None:
    """Flip each mismatched competitor row `comparable=false` / `CURRENCY_MISMATCH`.

    Only flips rows currently comparable — idempotent, a second run is a
    no-op (contracts/price-analysis-task.md step 4).
    """
    if not mismatched_match_ids:
        return
    session.execute(
        update(MatchCurrentPrice)
        .where(
            MatchCurrentPrice.workspace_id == workspace_id,
            MatchCurrentPrice.match_id.in_(mismatched_match_ids),
            MatchCurrentPrice.comparable.is_(True),
        )
        .values(comparable=False, error_code=ScrapeErrorCode.CURRENCY_MISMATCH)
    )


def _upsert_price_state(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    client_price,
    currency: str,
    outcome: AlertOutcome,
    latest_alert_state_id: uuid.UUID,
    now: datetime,
) -> None:
    """Upsert `variant_price_states` on `unique(workspace_id, product_variant_id)`.

    Deterministic body for identical inputs (SC-001) — only `calculated_at`/
    `updated_at` advance between runs. `latest_alert_state_id` (US2 T023)
    always points at the just-upserted `variant_alert_states` row for this
    variant, on every run (not only on a change).
    """
    values = {
        "workspace_id": workspace_id,
        "product_id": product_id,
        "product_variant_id": variant_id,
        "client_price": client_price,
        "currency": currency,
        "cheapest_competitor_price": outcome.cheapest,
        "average_competitor_price": outcome.average,
        "highest_competitor_price": outcome.highest,
        "comparable_competitor_count": outcome.comparable_count,
        "latest_alert_type": outcome.type,
        "latest_alert_severity": outcome.severity,
        "latest_alert_state_id": latest_alert_state_id,
        "calculated_at": now,
    }
    stmt = pg_insert(VariantPriceState).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["workspace_id", "product_variant_id"],
        set_={
            "product_id": stmt.excluded.product_id,
            "client_price": stmt.excluded.client_price,
            "currency": stmt.excluded.currency,
            "cheapest_competitor_price": stmt.excluded.cheapest_competitor_price,
            "average_competitor_price": stmt.excluded.average_competitor_price,
            "highest_competitor_price": stmt.excluded.highest_competitor_price,
            "comparable_competitor_count": stmt.excluded.comparable_competitor_count,
            "latest_alert_type": stmt.excluded.latest_alert_type,
            "latest_alert_severity": stmt.excluded.latest_alert_severity,
            "latest_alert_state_id": stmt.excluded.latest_alert_state_id,
            "calculated_at": stmt.excluded.calculated_at,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)


def _write_alert_event(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    alert_state_id: uuid.UUID,
    event_type: AlertEventType,
    previous_type: AlertType | None,
    new_type: AlertType,
    previous_severity: AlertSeverity | None,
    new_severity: AlertSeverity,
    message: str,
    details: dict | None,
    now: datetime,
) -> None:
    """Append one `price_alert_events` row (step 9, contracts/price-analysis-task.md).

    Called only when `engine.transition(...)` returned non-`None` — an
    UNCHANGED/no-event run never reaches here (US2 T023, FR-013/FR-014).
    """
    session.add(
        PriceAlertEvent(
            workspace_id=workspace_id,
            product_id=product_id,
            product_variant_id=variant_id,
            alert_state_id=alert_state_id,
            event_type=event_type,
            previous_type=previous_type,
            new_type=new_type,
            previous_severity=previous_severity,
            new_severity=new_severity,
            message=message,
            details=details,
            created_at=now,
        )
    )


def _load_prior_alert_state(
    session: Session, workspace_id: uuid.UUID, variant_id: uuid.UUID
) -> VariantAlertState | None:
    """The `unique(workspace_id, product_variant_id)` lookup for the current alert row."""
    return (
        session.execute(
            scoped_select(VariantAlertState, workspace_id).where(
                VariantAlertState.product_variant_id == variant_id
            )
        )
        .scalars()
        .one_or_none()
    )


def _upsert_alert_state(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    variant_id: uuid.UUID,
    client_price,
    outcome: AlertOutcome,
    prior: VariantAlertState | None,
    event_type,
    now: datetime,
) -> None:
    """Upsert `variant_alert_states` per contracts/price-analysis-task.md step 8.

    `status` ACTIVE iff `outcome.type` is non-NORMAL else RESOLVED;
    `first_seen_at` set to `now` on CREATED/REOPENED, else kept from
    `prior` (or `now` for the very first row, when there is no event —
    `prev is None and new is NORMAL`); `last_seen_at` always advanced;
    `resolved_at` set on RESOLVED, cleared on REOPENED, else kept.
    """
    status = AlertStatus.ACTIVE if outcome.type != AlertType.NORMAL else AlertStatus.RESOLVED

    if event_type in (AlertEventType.CREATED, AlertEventType.REOPENED) or prior is None:
        first_seen_at = now
    else:
        first_seen_at = prior.first_seen_at

    if event_type == AlertEventType.RESOLVED:
        resolved_at = now
    elif event_type == AlertEventType.REOPENED:
        resolved_at = None
    else:
        resolved_at = prior.resolved_at if prior is not None else None

    values = {
        "workspace_id": workspace_id,
        "product_id": product_id,
        "product_variant_id": variant_id,
        "type": outcome.type,
        "severity": outcome.severity,
        "status": status,
        "client_price": client_price,
        "benchmark_price": outcome.benchmark_price,
        "cheapest_competitor_price": outcome.cheapest,
        "average_competitor_price": outcome.average,
        "message": outcome.message,
        "details": outcome.details,
        "first_seen_at": first_seen_at,
        "last_seen_at": now,
        "resolved_at": resolved_at,
    }
    stmt = pg_insert(VariantAlertState).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["workspace_id", "product_variant_id"],
        set_={
            "product_id": stmt.excluded.product_id,
            "type": stmt.excluded.type,
            "severity": stmt.excluded.severity,
            "status": stmt.excluded.status,
            "client_price": stmt.excluded.client_price,
            "benchmark_price": stmt.excluded.benchmark_price,
            "cheapest_competitor_price": stmt.excluded.cheapest_competitor_price,
            "average_competitor_price": stmt.excluded.average_competitor_price,
            "message": stmt.excluded.message,
            "details": stmt.excluded.details,
            "first_seen_at": stmt.excluded.first_seen_at,
            "last_seen_at": stmt.excluded.last_seen_at,
            "resolved_at": stmt.excluded.resolved_at,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)


@app.task(name=PRICE_ANALYSIS_RECOMPUTE)
def recompute_variant(
    *,
    workspace_id: str,
    product_variant_id: str,
    product_id: str | None = None,
    scrape_job_id: str | None = None,
) -> None:
    """Recompute one variant's price comparison + alert state (idempotent).

    kwargs are JSON-serializable strings (Celery convention); `product_id`
    is optional (resolved from the variant if absent), `scrape_job_id` is
    accepted for signature parity with the emitters but unused here — the
    per-variant-per-job dedup is entirely an emission-side concern (D4).
    """
    del scrape_job_id  # dedup is emission-side only (D4/D7) — nothing to do with it here.

    ws = uuid.UUID(str(workspace_id))
    variant_id = uuid.UUID(str(product_variant_id))

    with get_session() as session:
        set_workspace_context(session, ws)

        variant = scoped_get(session, ProductVariant, variant_id, ws)
        if variant is None:
            return  # defensive no-op — a deleted/unknown variant.

        resolved_product_id = (
            uuid.UUID(str(product_id)) if product_id is not None else variant.product_id
        )

        competitor_rows = _load_competitor_rows(session, ws, variant_id)
        outcome = engine.analyze(variant.current_price, variant.currency, competitor_rows)

        _write_back_currency_mismatches(session, ws, outcome.mismatched_match_ids)

        now = datetime.now(timezone.utc)

        prior = _load_prior_alert_state(session, ws, variant_id)
        prev_type = prior.type if prior is not None else None
        prev_severity = prior.severity if prior is not None else None
        had_history = prior is not None

        event_type = engine.transition(
            prev_type, prev_severity, outcome.type, outcome.severity, had_history=had_history
        )

        _upsert_alert_state(
            session,
            workspace_id=ws,
            product_id=resolved_product_id,
            variant_id=variant_id,
            client_price=variant.current_price,
            outcome=outcome,
            prior=prior,
            event_type=event_type,
            now=now,
        )

        # Re-read the just-upserted row to recover its `id` (needed for
        # `variant_price_states.latest_alert_state_id` + the event's
        # `alert_state_id` FK-shaped reference) — the upsert above is a
        # Core statement, not an ORM-tracked insert, so its generated/
        # existing id isn't otherwise available without a RETURNING
        # round-trip. `unique(workspace_id, product_variant_id)`
        # guarantees exactly one row (US2 T023).
        alert_state = _load_prior_alert_state(session, ws, variant_id)
        assert alert_state is not None  # just upserted, above.

        _upsert_price_state(
            session,
            workspace_id=ws,
            product_id=resolved_product_id,
            variant_id=variant_id,
            client_price=variant.current_price,
            currency=variant.currency,
            outcome=outcome,
            latest_alert_state_id=alert_state.id,
            now=now,
        )

        if event_type is not None:
            _write_alert_event(
                session,
                workspace_id=ws,
                product_id=resolved_product_id,
                variant_id=variant_id,
                alert_state_id=alert_state.id,
                event_type=event_type,
                previous_type=prev_type,
                new_type=outcome.type,
                previous_severity=prev_severity,
                new_severity=outcome.severity,
                message=outcome.message,
                details=outcome.details,
                now=now,
            )

        session.commit()
