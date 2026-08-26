"""`maintenance` queue tasks — SPEC-15 retention/rollups/partition upkeep.

US1 (P1 MVP, contracts/partition-creation.md): ``partition_create`` keeps
current + next month's partitions in place for every *existing*
registered table (`app_shared.maintenance.registry.PARTITIONED_TABLES`),
self-healing and idempotent, so the calendar never causes a write outage
(SC-001). US2 (contracts/daily-rollup.md): ``daily_rollup`` upserts one
``variant_price_daily_rollups`` row per (workspace, variant, day) that
had observations that day. US3 (contracts/retention-drop.md):
``retention_drop`` drops whole expired monthly partitions via
``DROP TABLE`` — never bulk ``DELETE`` on a raw table — verifying daily-
rollup coverage first for ``price_observations`` (the only
``feeds_rollups`` table), and ages the small, non-partitioned
``variant_price_daily_rollups`` table via the one sanctioned bulk
``DELETE`` (R7).

All three maintenance tasks run on the BYPASSRLS system session
(`app_shared.database.get_system_session`) — the sanctioned SPEC-13
cross-tenant seam (research R9): partition ``CREATE``/``DROP`` DDL and
the rollup/retention cross-tenant source scans need an elevated role
under `FORCE ROW LEVEL SECURITY`. App-level workspace scoping is
preserved wherever a workspace-owned row is actually read/written —
`create_missing_partitions`/`run_retention` touch no workspace rows
(only DDL + catalog/coverage reads, plus the rollup table's age
``DELETE`` which is deliberately unscoped — it ages every workspace's
rollups past the same cutoff, R7); `run_daily_rollup` carries an
explicit ``workspace_id=`` on every rollup read/write (its one
cross-tenant scan, the driver query, is annotated ``# noqa:
workspace-scope`` at its source in ``app_shared.maintenance.rollups``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.workers.celery_app import app
from app_shared.config import get_settings
from app_shared.costauth import sweep_expired_reservations
from app_shared.database import get_system_session
from app_shared.maintenance.health import (
    EVENT_PARTITION_MISSING,
    find_missing_partitions,
)
from app_shared.maintenance.scoping import MaintenanceScope, maintenance_task
from app_shared.maintenance.partitions import create_missing_partitions
from app_shared.maintenance.retention import run_retention
from app_shared.maintenance.rollups import run_daily_rollup
from app_shared.maintenance.soft_refs import count_tolerated_dangling_refs
from app_shared.netledger.reconcile import (
    ReconciliationPolicyError,
    reconcile_window,
    windows_pending_reconciliation,
)
from app_shared.netledger.rollups import run_cost_rollup
from app_shared.task_names import (
    COSTAUTH_RESERVATION_SWEEP,
    MAINTENANCE_COST_ROLLUP,
    MAINTENANCE_DAILY_ROLLUP,
    MAINTENANCE_PARTITION_CREATE,
    MAINTENANCE_RECONCILE_PROVIDER_USAGE,
    MAINTENANCE_RETENTION_DROP,
)

logger = logging.getLogger("workers.maintenance")

#: ERROR-level structured event emitted when the BYPASSRLS system session
#: cannot be opened at all. 2026-08-15: the `worker` Railway service had
#: neither `SYSTEM_DATABASE_URL` nor `AUTH_DATABASE_URL`, so all three
#: maintenance tasks raised `RuntimeError` in `get_system_session()` on
#: every single run for a month. That *was* logged — as an anonymous
#: Celery traceback among thousands of INFO lines, mentioning only the
#: scheduler's "refresh-rule claim", which is not this task and not
#: greppable as a maintenance outage. This event name makes it one.
EVENT_SYSTEM_SESSION_UNAVAILABLE = "maintenance_system_session_unavailable"


@contextmanager
def _system_session(task_name: str) -> Iterator[Session]:
    """Open the BYPASSRLS system session, shouting first if it cannot be.

    The configuration is checked *before* entering
    :func:`get_system_session`, so the ERROR line is emitted and the
    original ``RuntimeError`` still propagates unchanged — the task must
    keep failing loudly (Celery records it), but the operator now gets a
    greppable line naming this task, the missing variable and the fix,
    instead of an unrelated traceback about the scheduler's refresh-rule
    claim.
    """
    settings = get_settings()
    if not (settings.SYSTEM_DATABASE_URL or settings.AUTH_DATABASE_URL):
        logger.error(
            "%s task=%s remedy=%s",
            EVENT_SYSTEM_SESSION_UNAVAILABLE,
            task_name,
            "set SYSTEM_DATABASE_URL (or AUTH_DATABASE_URL) on the worker service; "
            "without it EVERY maintenance task fails before doing any work, which "
            "silently stops partition creation, daily rollups and retention",
        )
    with get_system_session() as session:
        yield session


@maintenance_task(scope=MaintenanceScope.FLEET)
@app.task(name=MAINTENANCE_PARTITION_CREATE)
def partition_create() -> None:
    """`MAINTENANCE_PARTITION_CREATE` (`maintenance` queue,
    contracts/partition-creation.md, FR-004/005/006/007/008).

    Opens a BYPASSRLS system session, calls
    `create_missing_partitions` for the current time and
    `Settings.PARTITION_CREATE_LOOKAHEAD_MONTHS` months of lookahead,
    commits, and emits one structured run-report log line (FR-023) —
    `tables_skipped_absent` (e.g. `webhook_events` until SPEC-16, FR-002)
    and `partitions_created` (empty on a no-op re-run, FR-006).
    """
    settings = get_settings()
    now_utc = datetime.now(timezone.utc)
    lookahead = settings.PARTITION_CREATE_LOOKAHEAD_MONTHS
    with _system_session("partition_create") as session:
        report = create_missing_partitions(
            session,
            now_utc=now_utc,
            lookahead_months=lookahead,
        )
        session.commit()

        # Verify-after-create (2026-08-15 readiness cycle). Creation is
        # idempotent and best-effort per table, so a run can "succeed"
        # while a required partition is still absent (a table locked by
        # another session, a permission problem, a registry entry whose
        # parent was recreated). Re-reading the catalog after the commit
        # is the only statement that can honestly say the calendar is
        # safe, and it costs one cheap `to_regclass` probe per partition.
        still_missing, _skipped = find_missing_partitions(
            session, now_utc=now_utc, months_ahead=lookahead
        )

    for missing in still_missing:
        logger.error(
            "%s table=%s partition=%s month_start=%s days_until_writes_fail=%d "
            "source=partition_create_verify",
            EVENT_PARTITION_MISSING,
            missing.table,
            missing.partition,
            missing.month_start.date().isoformat(),
            missing.days_until_writes_fail(now_utc),
        )

    logger.info(
        "maintenance_partition_create tables_skipped_absent=%s partitions_created=%s "
        "lookahead_months=%d partitions_still_missing=%s",
        report.tables_skipped_absent,
        report.partitions_created,
        lookahead,
        [m.partition for m in still_missing],
    )


@maintenance_task(scope=MaintenanceScope.FLEET)
@app.task(name=MAINTENANCE_DAILY_ROLLUP)
def daily_rollup(target_date: str | None = None) -> None:
    """`MAINTENANCE_DAILY_ROLLUP` (`maintenance` queue,
    contracts/daily-rollup.md, FR-009/010/011/012/013/014).

    Opens a BYPASSRLS system session, calls `run_daily_rollup` for
    ``target_date`` (an ISO ``YYYY-MM-DD`` string, e.g. for an explicit
    backfill day — defaults to yesterday UTC when omitted, the normal
    scheduler-cadence call shape), commits, and emits one structured
    run-report log line (FR-023) — `rollups_upserted` and
    `variants_skipped_no_state` (a variant with observations that day but
    no SPEC-09 `variant_price_states` row yet).
    """
    parsed_date = date.fromisoformat(target_date) if target_date is not None else None
    with _system_session("daily_rollup") as session:
        report = run_daily_rollup(session, target_date=parsed_date)
        session.commit()

    logger.info(
        "maintenance_daily_rollup rollups_upserted=%s variants_skipped_no_state=%s",
        report.rollups_upserted,
        report.variants_skipped_no_state,
    )


@maintenance_task(scope=MaintenanceScope.FLEET)
@app.task(name=MAINTENANCE_RETENTION_DROP)
def retention_drop() -> None:
    """`MAINTENANCE_RETENTION_DROP` (`maintenance` queue,
    contracts/retention-drop.md, FR-015/016/017/018/019/020).

    Opens a BYPASSRLS system session, calls `run_retention` for the
    current time, commits, and emits one structured run-report log line
    (FR-023) — `tables_skipped_absent` (e.g. `webhook_events` until
    SPEC-16, FR-002), `partitions_dropped` (whole expired partitions
    reclaimed via `DROP TABLE`, never bulk `DELETE` on a raw table,
    FR-015), `partitions_skipped_pending_rollups` (an expired
    `price_observations` partition retained because its daily-rollup
    coverage is incomplete, FR-016), and `rollup_rows_deleted` (the one
    sanctioned bulk `DELETE` aging the non-partitioned
    `variant_price_daily_rollups` table, R7). `dangling_soft_refs_tolerated`
    (US4/T032/T033, contracts/soft-reference-tolerance.md) is a best-effort
    operator-visibility count of `match_current_prices` rows whose
    `observation_id` no longer resolves — an *expected*, tolerated
    condition, never corruption (FR-022) — logged as `None` if the probe
    itself fails; this optional check can NEVER block or fail the core
    create/rollup/drop guarantees above it (FR-024's non-blocking
    principle, applied here).
    """
    with _system_session("retention_drop") as session:
        report = run_retention(session, now_utc=datetime.now(timezone.utc))

        dangling_soft_refs_tolerated: int | None
        try:
            dangling_soft_refs_tolerated = count_tolerated_dangling_refs(session)
        except Exception:  # noqa: BLE001 - best-effort operator-visibility probe only (FR-022/024)
            logger.warning(
                "maintenance_retention_drop dangling_soft_refs_tolerated probe failed",
                exc_info=True,
            )
            dangling_soft_refs_tolerated = None

        session.commit()

    logger.info(
        "maintenance_retention_drop tables_skipped_absent=%s partitions_dropped=%s "
        "partitions_skipped_pending_rollups=%s rollup_rows_deleted=%s "
        "dangling_soft_refs_tolerated=%s",
        report.tables_skipped_absent,
        report.partitions_dropped,
        report.partitions_skipped_pending_rollups,
        report.rollup_rows_deleted,
        dangling_soft_refs_tolerated,
    )


@maintenance_task(scope=MaintenanceScope.FLEET)
@app.task(name=COSTAUTH_RESERVATION_SWEEP)
def costauth_reservation_sweep() -> None:
    """`COSTAUTH_RESERVATION_SWEEP` (`maintenance` queue, EPA C3, READY-006).

    Reaps EXPIRED cost-authorization leases — the one failure mode the
    dispatch paths' own `release()` calls cannot cover. A worker that is
    killed between `authorize()` and its POST leaves a `RESERVED` row
    holding budget with nobody left to release it; without this sweep
    that money is held until an operator notices.

    **It is not a TTL expiry.** For every expired lease the sweeper first
    asks C1's ledger whether an operation opened under that
    `authorization_id` is still open (`network_operations.closed_at IS
    NULL`). If one is, the reservation stays `RESERVED` and its lease is
    extended: an expired lease is evidence that a *heartbeat* stopped —
    which happens whenever a worker is paused, throttled or merely slow —
    and treating it as evidence that the *work* stopped is exactly how a
    budget gets spent twice while its counters look healthy.

    FLEET-scoped and run on the BYPASSRLS system session, because the
    sweep is inherently cross-tenant (one pass must see every workspace's
    expired reservations) and must additionally read
    `network_operations`, which has no `workspace_id` to scope by at all.
    Same seam, for the same reason, as C1's allocation writes and the
    scheduler's due-rule claim.

    Idempotent and no-arg: every release is a compare-and-set on the
    reservation's own state, so a duplicate delivery releases nothing
    twice and two sweepers running at once contend on nothing (the scan
    takes `FOR UPDATE SKIP LOCKED`).
    """
    with _system_session("costauth_reservation_sweep") as session:
        released, skipped_live = sweep_expired_reservations(
            session, now=datetime.now(timezone.utc)
        )

    logger.info(
        "maintenance_costauth_reservation_sweep released=%d skipped_live=%d",
        released,
        skipped_live,
    )


@maintenance_task(scope=MaintenanceScope.FLEET)
@app.task(name=MAINTENANCE_RECONCILE_PROVIDER_USAGE)
def reconcile_provider_usage(target_date: str | None = None, provider: str | None = None) -> None:
    """`MAINTENANCE_RECONCILE_PROVIDER_USAGE` (`maintenance` queue, EPA C5,
    READY-005 part 2).

    Per provider/day/account: reconciles whatever provider usage has
    ALREADY been imported (``scripts/import_dataimpulse_usage.py`` —
    file-based, no network call, run by an operator once the owner
    supplies an export) against C1's `network_operations` ledger for one
    UTC day, appending `network_operation_settlements` versions where
    bytes reconcile within tolerance (`app_shared.netledger.reconcile.
    reconcile_window`). This task does not itself talk to any provider —
    it only reads rows already sitting in `provider_usage_records`.

    * **Cadence (no `target_date`)** — the scheduler's daily call.
      Reconciles the PRIOR UTC day (`target_date - 1`), so a day's
      provider export (typically pulled the following morning) has had a
      full day to land before this task looks for it. Missing evidence
      for a day is not an error here — `windows_pending_reconciliation`
      simply returns nothing for a day nobody has imported yet, and the
      next run (or a later explicit backfill call) picks it up once the
      import exists.
    * **One explicit day** (`target_date`, ISO `YYYY-MM-DD`) — a manual
      backfill/replay call, e.g. once the owner finally supplies the
      pending DataImpulse export for the 2026-08-24 canary window.
    * **`provider`** (optional) narrows to one provider; omitted means
      every provider with imported evidence for the day.

    Each `(provider, provider_account, window)` triple found is
    reconciled independently and NEVER lets one window's failure (a
    `ReconciliationPolicyError` — e.g. a provider account whose rows
    disagree on currency, see that module's policy) abort the others;
    the failure is logged and counted, and every other window still
    gets its chance. Idempotent by construction (`reconcile_window`
    skips a settlement it would otherwise duplicate) — a retried or
    doubly-scheduled run costs nothing extra.

    Emits one structured run-report log line (FR-023 convention) —
    `windows_reconciled`, `windows_passed`, `windows_failed_open_gap`
    (a window whose report came back with unexplained usage — the
    147-vs-70 canary shape) and `windows_errored` (a policy violation or
    unexpected failure, named so an operator does not have to grep for
    it among ordinary INFO lines, the same EVENT_SYSTEM_SESSION_UNAVAILABLE
    lesson this module already learned once).
    """
    if target_date is not None:
        parsed_date = date.fromisoformat(target_date)
    else:
        parsed_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    with _system_session("reconcile_provider_usage") as session:
        windows = windows_pending_reconciliation(
            session, target_date=parsed_date, provider=provider
        )

    windows_passed = 0
    windows_failed_open_gap = 0
    windows_errored = 0
    for window in windows:
        try:
            report = reconcile_window(window)
        except ReconciliationPolicyError:
            windows_errored += 1
            logger.error(
                "maintenance_reconcile_provider_usage_policy_error "
                "window_id=%s provider=%s provider_account=%s",
                window.id,
                window.provider,
                window.provider_account,
                exc_info=True,
            )
            continue
        except Exception:  # noqa: BLE001 - one bad window must not abort the rest
            windows_errored += 1
            logger.error(
                "maintenance_reconcile_provider_usage_unexpected_error "
                "window_id=%s provider=%s provider_account=%s",
                window.id,
                window.provider,
                window.provider_account,
                exc_info=True,
            )
            continue

        if report.passed:
            windows_passed += 1
        else:
            windows_failed_open_gap += 1
            logger.warning(
                "maintenance_reconcile_provider_usage_open_gap window_id=%s "
                "provider=%s provider_account=%s app_requests=%d provider_requests=%d "
                "app_bytes=%d provider_bytes=%d unexplained=%d",
                window.id,
                window.provider,
                window.provider_account,
                report.app_requests,
                report.provider_requests,
                report.app_bytes,
                report.provider_bytes,
                len(report.unexplained_operations),
            )

    logger.info(
        "maintenance_reconcile_provider_usage target_date=%s provider=%s "
        "windows_reconciled=%d windows_passed=%d windows_failed_open_gap=%d "
        "windows_errored=%d",
        parsed_date.isoformat(),
        provider,
        len(windows),
        windows_passed,
        windows_failed_open_gap,
        windows_errored,
    )


@maintenance_task(scope=MaintenanceScope.FLEET)
@app.task(name=MAINTENANCE_COST_ROLLUP)
def cost_rollup(target_date: str | None = None) -> None:
    """`MAINTENANCE_COST_ROLLUP` (`maintenance` queue, EPA C6).

    Maintains the bounded, durable ``network_cost_rollups``/
    ``fleet_network_cost_rollups`` tables
    (`app_shared.netledger.rollups.run_cost_rollup`) that `GET
    /ops/metrics`'s `cost_rollup` section and the tenant-scoped `GET
    /v1/cost-rollups` both read — neither ever aggregates the raw
    `network_operations`/`network_operation_allocations` ledger
    synchronously on request.

    * **Cadence (no `target_date`)** — durable-watermark catch-up
      (mirrors `daily_rollup`'s own cadence path): every UTC day owed
      since the `rollup_watermarks` cursor (key
      `app_shared.netledger.rollups.WATERMARK_COST_ROLLUP`), oldest
      first, bounded per call. `run_cost_rollup` commits each day's
      upserts + watermark advance together internally
      (`commit=True`, its default) — the same no-loss/no-double-count
      ordering `run_rollup_catchup` uses, so this task issues no extra
      commit of its own in this mode.
    * **One explicit day** (`target_date`, ISO `YYYY-MM-DD`) — a manual
      backfill/replay call. `run_cost_rollup` does not commit in this
      mode (mirrors `run_daily_rollup`'s explicit-day mode), so this
      task commits once, itself, after the call.

    Emits one structured run-report log line (FR-023 convention) —
    `days_processed`, `fleet_rows_upserted`, `tenant_rows_upserted`,
    `watermark_store_available`, `watermark_advanced`.
    """
    parsed_date = date.fromisoformat(target_date) if target_date is not None else None

    with _system_session("cost_rollup") as session:
        report = run_cost_rollup(session, target_date=parsed_date)
        if parsed_date is not None:
            session.commit()

    logger.info(
        "maintenance_cost_rollup target_date=%s days_processed=%s "
        "fleet_rows_upserted=%d tenant_rows_upserted=%d "
        "watermark_store_available=%s watermark_advanced=%s",
        target_date,
        [d.isoformat() for d in report.days_processed],
        report.fleet_rows_upserted,
        report.tenant_rows_upserted,
        report.watermark_store_available,
        report.watermark_advanced,
    )
