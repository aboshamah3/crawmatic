"""Daily cost and freshness scorecard (EPA D5, deep dive §12 item 9).

``MAINTENANCE_DAILY_SCORECARD`` writes one ``fleet_daily_scorecard`` row
per UTC day, carrying every field the plan names:
``provider_bytes``, ``railway_cpu_seconds``, ``railway_ram_gb_hours``,
``railway_egress_gb``, ``valid_fresh_matches``, ``attempts_per_valid_fresh``,
``browser_share``, ``proxied_share``, ``queue_oldest_seconds_p95``,
``persistence_lag_seconds_p95``, ``missing_metric_fraction``,
``budget_reserved_usd``, ``budget_settled_usd``, ``backup_egress_gb``,
``cost_per_valid_fresh_micro_usd``.

THE CENTRAL DISCIPLINE: NULL, NEVER 0, FOR AN UNMEASURED INPUT
----------------------------------------------------------------
A metric this task could not compute for the day — no rows in the
window, a table not yet populated, a source that genuinely does not
exist yet (see "Railway platform metrics" below) — is written as
``NULL``. It is never coerced to ``0``, because a scorecard reader
cannot tell "confirmed zero" from "we have no idea" once a fabricated
zero has landed in the column; that ambiguity is exactly what a cost/
freshness gate cannot afford. Every query below follows the same
per-metric-degrades-independently discipline
``app_shared.opsmetrics.emit.collect_baseline_metrics`` established: one
group failing (a missing table, a locked partition) must not blind the
rest of the row.

CALENDAR-DAY, NOT ROLLING-WINDOW
---------------------------------
Every query is bounded by an explicit ``[day_start, day_end)`` UTC
window, not ``now() - interval '24 hours'``. A "daily" scorecard row
must mean the same thing regardless of what time of day the task
happens to run, and must be safe to recompute (the write is an
idempotent UPSERT on ``date``) — a rolling window would give a different
answer for the "same" day depending on invocation time, which is useless
for a historical record. This intentionally does NOT reuse
``app_shared.opsmetrics.emit.collect_baseline_metrics`` (whose SQL is
the ``now() - interval`` shape B9/D5's own gauges use for live
alerting) — the two answer different questions from the same underlying
facts, and are deliberately two statements rather than one parameterised
one, so a change to the live alerting window can never silently change
a day's already-written historical row.

WHAT "QUEUE" AND "PERSISTENCE LAG" MEAN HERE
----------------------------------------------
The plan's ``queue_oldest_seconds_p95`` and ``persistence_lag_seconds_p95``
are read from the same three-phase-lifecycle p95 the A5 baseline gauges
already compute for ``scrape_job_targets`` (``created_at`` ->
``dispatched_at`` -> ``first_network_at`` -> ``persisted_at``,
``app_shared.opsmetrics.emit.TARGET_PHASE_P95_SECONDS_SQL``):

* ``queue_oldest_seconds_p95``     <- ``due_to_dispatch`` phase p95 (how
  long a target waited in the queue before being picked up, p95 over the
  day) -- the closest day-scoped p95 to a live "oldest pending" gauge
  that ANY existing table can answer; no time-series sample store exists
  to p95 a point-in-time gauge across the day, so that literal reading
  is not computable and this is the deliberate, documented substitute.
* ``persistence_lag_seconds_p95``  <- ``first_network_to_persisted``
  phase p95 (time from first network contact to durably persisted) --
  an exact naming match, no substitution involved.

RAILWAY PLATFORM METRICS: ALWAYS NULL TODAY
----------------------------------------------
``railway_cpu_seconds``/``railway_ram_gb_hours``/``railway_egress_gb``
are Railway platform-billing figures. Per the D5 packet's own note, this
task does not call the Railway API; it reads "the existing watchdog's
stored output" if one exists. Checked:
``app_shared.memory_watchdog`` (the only existing watchdog) enforces a
live RSS ceiling and persists nothing -- there is no stored Railway
usage figure anywhere in this codebase today. All three columns are
therefore always ``NULL`` until a future task adds a durable store for
them (e.g. an owner-run importer of Railway's own usage export, the
same shape as ``scripts/import_dataimpulse_usage.py``). Documented, not
silently absent: :data:`RAILWAY_METRICS_UNAVAILABLE_REASON`.

BACKUP EGRESS: FED BY THE C10 RECEIVER, THROUGH REDIS
----------------------------------------------------------
``POST /admin/ops/backup-report`` (``apps/api/app/routers/admin_ops.py``)
is the receiver C10's ``dr_post_backup_report`` posts to. It has no
table of its own -- adding one would be a second Alembic revision in a
stage the packet caps at one -- so a day's received reports are folded,
via atomic ``HINCRBY``, into one Redis hash keyed by the UTC day the
report's own ``created_utc`` falls in
(:func:`backup_report_redis_key`). :func:`record_backup_report` is the
write side (called by the route on every accepted POST);
:func:`backup_egress_gb_for_day` is the read side this module calls
while assembling a row. Only a leg with ``private_network: false``
contributes bytes -- a private-network leg costs no egress by
construction (that is the entire point of C10), so counting it would
hide the exact regression this figure exists to catch. The Redis key
carries a generous TTL (:data:`BACKUP_REPORT_REDIS_TTL_SECONDS`, 45
days) -- long enough that this task is never racing its own retry
window, short enough that a permanently-down Redis does not accumulate
keys forever. If Redis is unavailable, or no report was received for
the day, ``backup_egress_gb`` is ``NULL`` -- an absent report says
nothing about egress, not that egress was zero.

Scraping-free (Constitution I/V) -- SQLAlchemy + stdlib only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = [
    "BACKUP_REPORT_REDIS_TTL_SECONDS",
    "BACKUP_REPORT_SCHEMA",
    "MICRO_UNITS_PER_USD",
    "RAILWAY_METRICS_UNAVAILABLE_REASON",
    "ScorecardRow",
    "backup_egress_gb_for_day",
    "backup_report_redis_key",
    "compute_scorecard",
    "read_scorecard",
    "read_scorecard_range",
    "record_backup_report",
    "run_daily_scorecard",
    "upsert_scorecard",
]

logger = logging.getLogger(__name__)

#: 1 USD == 1,000,000 micro-USD -- the repo-wide money unit
#: (`app_shared.costauth.service.MICRO_UNITS_PER_USD`, re-declared here
#: rather than imported to avoid a maintenance-layer -> costauth import
#: for one integer; the two are pinned equal by
#: `tests/unit/test_scorecard_fields.py`).
MICRO_UNITS_PER_USD = 1_000_000

#: Why the three Railway platform-billing columns are always NULL today
#: -- see the module docstring's "RAILWAY PLATFORM METRICS" section.
RAILWAY_METRICS_UNAVAILABLE_REASON = (
    "no durable store of Railway usage-API figures exists yet "
    "(app_shared.memory_watchdog persists nothing); wire an importer "
    "before these columns can carry a value"
)

# --- The backup-report side channel (Redis) --------------------------------

#: Must match `scripts/dr/dr_lib.sh`'s `$DR_REPORT_SCHEMA` exactly -- the
#: receiver (`POST /admin/ops/backup-report`) rejects anything else so a
#: shape it predates cannot be silently mis-recorded.
BACKUP_REPORT_SCHEMA = "crawmatic.backup-report.v1"

#: How long a day's Redis aggregate survives. Generous relative to the
#: daily cadence this task runs on: a day's key must still be there the
#: next time the scorecard task looks at it (worst case, the task was
#: down for a while and is catching up), but must not accumulate
#: forever if nothing ever reads it.
BACKUP_REPORT_REDIS_TTL_SECONDS = 45 * 86400


def backup_report_redis_key(day: date_type) -> str:
    """The Redis hash key holding day ``day``'s folded backup reports."""
    return f"backup_report:daily:{day.isoformat()}"


def _parse_report_day(payload: dict[str, Any], *, now: datetime) -> date_type:
    """The UTC calendar day a backup-report payload belongs to.

    Read from the payload's own ``created_utc`` (the day the BACKUP was
    taken, not the day the POST happened to arrive) so a delayed or
    retried post still lands on the right day. Falls back to ``now``'s
    date on anything unparsable -- a malformed timestamp must not crash
    the receiver; it must record something, not nothing.
    """
    raw = payload.get("created_utc")
    if isinstance(raw, str) and raw:
        try:
            cleaned = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            return datetime.fromisoformat(cleaned).astimezone(timezone.utc).date()
        except ValueError:
            pass
    return now.date()


def record_backup_report(
    redis_client: Any,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Fold one accepted backup-report payload into its day's Redis hash.

    Returns ``True`` iff the write happened. ``False`` (never raises) on
    a schema mismatch, a malformed payload, an absent Redis client, or a
    Redis error -- this is a monitoring input, not the backup itself;
    losing one report must never surface as a 5xx to
    ``dr_post_backup_report``, which is itself fail-soft on the other
    end (a non-2xx there just means "kept locally, logged a WARN").
    """
    if redis_client is None:
        return False
    if not isinstance(payload, dict) or payload.get("schema") != BACKUP_REPORT_SCHEMA:
        return False
    now = now or datetime.now(timezone.utc)
    day = _parse_report_day(payload, now=now)
    key = backup_report_redis_key(day)
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    try:
        bytes_on_wire = int(totals.get("bytes_on_wire") or 0)
    except (TypeError, ValueError):
        bytes_on_wire = 0
    private = bool(payload.get("private_network", True))

    try:
        pipe = redis_client.pipeline()
        pipe.hincrby(key, "reports", 1)
        pipe.hincrby(key, "public_bytes_on_wire", 0 if private else bytes_on_wire)
        pipe.hset(key, "any_private" if private else "any_public", "1")
        pipe.hset(key, "last_created_utc", str(payload.get("created_utc") or ""))
        pipe.hset(key, "last_backup_set", str(payload.get("backup_set") or ""))
        pipe.expire(key, BACKUP_REPORT_REDIS_TTL_SECONDS)
        pipe.execute()
        return True
    except Exception:  # noqa: BLE001 - a monitoring input, never fatal
        logger.warning(
            "scorecard_backup_report_record_failed day=%s", day.isoformat(),
            exc_info=True,
        )
        return False


def backup_egress_gb_for_day(redis_client: Any, day: date_type) -> float | None:
    """GB moved on the wire by a non-private-network backup leg on ``day``.

    ``None`` when Redis is unavailable or no report was received for the
    day at all -- an absent report is a missing INPUT, not an observed
    zero. ``0.0`` is a legitimate answer: it means reports WERE received
    and every one of them was over the private network (the success
    case C10 exists to produce).
    """
    if redis_client is None:
        return None
    try:
        raw = redis_client.hgetall(backup_report_redis_key(day))
    except Exception:  # noqa: BLE001 - degrade to "unmeasured", never raise
        return None
    if not raw:
        return None

    def _get(field: str) -> Any:
        if field in raw:
            return raw[field]
        encoded = field.encode() if isinstance(field, str) else field
        return raw.get(encoded)

    reports = _get("reports")
    if not reports or int(reports) <= 0:
        return None
    public_bytes = _get("public_bytes_on_wire") or 0
    return int(public_bytes) / 1_000_000_000


# --- Calendar-day-bound SQL -------------------------------------------------
#
# Every statement below takes `:day_start`/`:day_end` (UTC, [start, end))
# and degrades independently -- see the module docstring.

_VALID_FRESH_MATCHES_SQL = text(
    """
    SELECT COUNT(DISTINCT match_id) AS valid_fresh_matches
    FROM match_current_prices
    WHERE updated_at >= :day_start AND updated_at < :day_end
    """
)

_ATTEMPTS_SQL = text(
    """
    SELECT COUNT(*) AS attempts
    FROM request_attempts
    WHERE created_at >= :day_start AND created_at < :day_end
    """
)

#: Top-level physical operations only (`parent_operation_id IS NULL`) --
#: a fleet-wide daily browser/proxy MIX gauge, not a billing figure.
#: Unlike C6's exact per-tenant billing predicate (which also folds in
#: subresource children via a parent/child join), this scorecard column
#: deliberately scopes to top-level operations for one Postgres
#: statement with no CTE -- the mix a top-level fetch took is
#: representative of the mix the fleet chose that day, and children
#: inherit their parent's transport/provider by construction, not by
#: an independent decision this gauge needs to re-examine.
_TRANSPORT_SHARE_SQL = text(
    """
    SELECT
        COUNT(*) AS total_ops,
        COUNT(*) FILTER (WHERE no.transport = 'BROWSER') AS browser_ops,
        COUNT(*) FILTER (
            WHERE no.provider <> 'direct' AND ra.proxy_provider_id IS NOT NULL
        ) AS proxied_ops
    FROM network_operations no
    LEFT JOIN request_attempts ra ON ra.network_operation_id = no.network_request_id
    WHERE no.created_at >= :day_start AND no.created_at < :day_end
      AND no.parent_operation_id IS NULL
    """
)

#: p95 of two `scrape_job_targets` lifecycle phases, over targets CREATED
#: in the day -- see the module docstring for why these two phases are
#: what `queue_oldest_seconds_p95`/`persistence_lag_seconds_p95` read.
#: `percentile_cont` (interpolating), each phase `FILTER`ed to rows where
#: both of its boundaries are non-NULL -- the same shape and rationale as
#: `app_shared.opsmetrics.emit.TARGET_PHASE_P95_SECONDS_SQL`.
_TARGET_PHASE_P95_SQL = text(
    """
    SELECT
        percentile_cont(0.95) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (dispatched_at - created_at))
        ) FILTER (WHERE dispatched_at IS NOT NULL) AS queue_oldest_seconds_p95,
        percentile_cont(0.95) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (persisted_at - first_network_at))
        ) FILTER (
            WHERE persisted_at IS NOT NULL AND first_network_at IS NOT NULL
        ) AS persistence_lag_seconds_p95
    FROM scrape_job_targets
    WHERE created_at >= :day_start AND created_at < :day_end
    """
)

_PROVIDER_BYTES_SQL = text(
    """
    SELECT SUM(total_bytes) AS provider_bytes
    FROM provider_usage_records
    WHERE occurred_at >= :day_start AND occurred_at < :day_end
    """
)

_BUDGET_RESERVED_SQL = text(
    """
    SELECT SUM(reserved_cost_micro_units) AS reserved_micro
    FROM cost_reservations
    WHERE created_at >= :day_start AND created_at < :day_end
    """
)

_BUDGET_SETTLED_SQL = text(
    """
    SELECT SUM(settled_cost_micro_units) AS settled_micro
    FROM cost_reservations
    WHERE state = 'SETTLED'
      AND settled_at >= :day_start AND settled_at < :day_end
    """
)


def _read_one(session: Session, group: str, stmt: Any, params: dict[str, Any]) -> Any:
    """Run one statement, degrading to ``None`` (never raising) on failure.

    Mirrors `app_shared.opsmetrics.emit.collect_baseline_metrics._read`:
    a missing table or a locked partition must not blind the rest of the
    row, and a failed statement poisons the surrounding Postgres
    transaction unless rolled back before the next group runs.
    """
    try:
        return session.execute(stmt, params).one()
    except Exception:  # noqa: BLE001 - degrade, never raise
        logger.warning("scorecard_metric_unavailable group=%s", group, exc_info=True)
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            try:
                rollback()
            except Exception:  # noqa: BLE001 - best effort only
                pass
        return None


@dataclass(frozen=True)
class ScorecardRow:
    """One computed (not yet written) ``fleet_daily_scorecard`` row.

    Every field is ``None`` unless it was genuinely measured for
    ``date`` -- see the module docstring. ``missing_metric_fraction`` is
    the one field always populated once a row exists at all: it is
    computed FROM the other 13's None-ness, not itself a raw input.
    """

    date: date_type
    provider_bytes: int | None = None
    railway_cpu_seconds: float | None = None
    railway_ram_gb_hours: float | None = None
    railway_egress_gb: float | None = None
    valid_fresh_matches: int | None = None
    attempts_per_valid_fresh: float | None = None
    browser_share: float | None = None
    proxied_share: float | None = None
    queue_oldest_seconds_p95: float | None = None
    persistence_lag_seconds_p95: float | None = None
    missing_metric_fraction: float | None = None
    budget_reserved_usd: float | None = None
    budget_settled_usd: float | None = None
    backup_egress_gb: float | None = None
    cost_per_valid_fresh_micro_usd: float | None = None

    #: Every column name EXCEPT `date` and `missing_metric_fraction`
    #: itself -- the denominator/inputs `missing_metric_fraction` is
    #: computed over. A tuple (not a set) so the completeness fraction is
    #: reproducible/orderable in tests. `ClassVar` so dataclass does not
    #: treat this shared constant as a per-instance field.
    MEASURED_FIELDS: ClassVar[tuple[str, ...]] = (
        "provider_bytes",
        "railway_cpu_seconds",
        "railway_ram_gb_hours",
        "railway_egress_gb",
        "valid_fresh_matches",
        "attempts_per_valid_fresh",
        "browser_share",
        "proxied_share",
        "queue_oldest_seconds_p95",
        "persistence_lag_seconds_p95",
        "budget_reserved_usd",
        "budget_settled_usd",
        "backup_egress_gb",
        "cost_per_valid_fresh_micro_usd",
    )

    def as_dict_raw(self) -> dict[str, Any]:
        """Every field as native Python values (``date`` stays a
        ``date``) -- the shape :func:`upsert_scorecard`'s bind
        parameters and this class's own reconstruction need. Use
        :meth:`as_dict` instead for a JSON-facing payload."""
        return {
            "date": self.date,
            "provider_bytes": self.provider_bytes,
            "railway_cpu_seconds": self.railway_cpu_seconds,
            "railway_ram_gb_hours": self.railway_ram_gb_hours,
            "railway_egress_gb": self.railway_egress_gb,
            "valid_fresh_matches": self.valid_fresh_matches,
            "attempts_per_valid_fresh": self.attempts_per_valid_fresh,
            "browser_share": self.browser_share,
            "proxied_share": self.proxied_share,
            "queue_oldest_seconds_p95": self.queue_oldest_seconds_p95,
            "persistence_lag_seconds_p95": self.persistence_lag_seconds_p95,
            "missing_metric_fraction": self.missing_metric_fraction,
            "budget_reserved_usd": self.budget_reserved_usd,
            "budget_settled_usd": self.budget_settled_usd,
            "backup_egress_gb": self.backup_egress_gb,
            "cost_per_valid_fresh_micro_usd": self.cost_per_valid_fresh_micro_usd,
        }

    def as_dict(self) -> dict[str, Any]:
        """JSON-facing payload (``date`` serialised to an ISO string) --
        what `GET /admin/scorecard` returns per row."""
        payload = self.as_dict_raw()
        payload["date"] = self.date.isoformat()
        return payload


def compute_scorecard(
    session: Session,
    day: date_type,
    *,
    redis_client: Any | None = None,
) -> ScorecardRow:
    """Compute (never writes) day ``day``'s scorecard row.

    Runs on whatever ``session`` it is handed -- the caller
    (:func:`run_daily_scorecard`) is responsible for that being the
    BYPASSRLS system session, exactly like every other maintenance sweep
    in this package (research R9): every query here is a fleet-wide
    aggregate across every workspace by construction, so there is no
    workspace to scope it to.
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    params = {"day_start": day_start, "day_end": day_end}

    valid_fresh_matches: int | None = None
    row = _read_one(session, "valid_fresh_matches", _VALID_FRESH_MATCHES_SQL, params)
    if row is not None:
        valid_fresh_matches = int(row.valid_fresh_matches or 0)

    attempts_per_valid_fresh: float | None = None
    row = _read_one(session, "attempts", _ATTEMPTS_SQL, params)
    if row is not None and valid_fresh_matches:
        attempts_per_valid_fresh = float(row.attempts) / valid_fresh_matches

    browser_share: float | None = None
    proxied_share: float | None = None
    row = _read_one(session, "transport_share", _TRANSPORT_SHARE_SQL, params)
    if row is not None and row.total_ops:
        browser_share = float(row.browser_ops or 0) / row.total_ops
        proxied_share = float(row.proxied_ops or 0) / row.total_ops

    queue_oldest_seconds_p95: float | None = None
    persistence_lag_seconds_p95: float | None = None
    row = _read_one(session, "target_phase_p95", _TARGET_PHASE_P95_SQL, params)
    if row is not None:
        queue_oldest_seconds_p95 = (
            None
            if row.queue_oldest_seconds_p95 is None
            else float(row.queue_oldest_seconds_p95)
        )
        persistence_lag_seconds_p95 = (
            None
            if row.persistence_lag_seconds_p95 is None
            else float(row.persistence_lag_seconds_p95)
        )

    provider_bytes: int | None = None
    row = _read_one(session, "provider_bytes", _PROVIDER_BYTES_SQL, params)
    if row is not None and row.provider_bytes is not None:
        provider_bytes = int(row.provider_bytes)

    budget_reserved_usd: float | None = None
    row = _read_one(session, "budget_reserved", _BUDGET_RESERVED_SQL, params)
    if row is not None and row.reserved_micro is not None:
        budget_reserved_usd = float(row.reserved_micro) / MICRO_UNITS_PER_USD

    budget_settled_usd: float | None = None
    settled_micro: int | None = None
    row = _read_one(session, "budget_settled", _BUDGET_SETTLED_SQL, params)
    if row is not None and row.settled_micro is not None:
        settled_micro = int(row.settled_micro)
        budget_settled_usd = settled_micro / MICRO_UNITS_PER_USD

    cost_per_valid_fresh_micro_usd: float | None = None
    if settled_micro is not None and valid_fresh_matches:
        cost_per_valid_fresh_micro_usd = settled_micro / valid_fresh_matches

    backup_egress_gb = backup_egress_gb_for_day(redis_client, day)

    partial = ScorecardRow(
        date=day,
        provider_bytes=provider_bytes,
        railway_cpu_seconds=None,
        railway_ram_gb_hours=None,
        railway_egress_gb=None,
        valid_fresh_matches=valid_fresh_matches,
        attempts_per_valid_fresh=attempts_per_valid_fresh,
        browser_share=browser_share,
        proxied_share=proxied_share,
        queue_oldest_seconds_p95=queue_oldest_seconds_p95,
        persistence_lag_seconds_p95=persistence_lag_seconds_p95,
        budget_reserved_usd=budget_reserved_usd,
        budget_settled_usd=budget_settled_usd,
        backup_egress_gb=backup_egress_gb,
        cost_per_valid_fresh_micro_usd=cost_per_valid_fresh_micro_usd,
    )
    missing = sum(
        1 for field in ScorecardRow.MEASURED_FIELDS if getattr(partial, field) is None
    )
    missing_metric_fraction = missing / len(ScorecardRow.MEASURED_FIELDS)
    raw = partial.as_dict_raw()
    raw["missing_metric_fraction"] = missing_metric_fraction
    return ScorecardRow(**raw)


# --- Durable read/write on `fleet_daily_scorecard` --------------------------
#
# Raw `text()` statements rather than the ORM, the same convention
# `app_shared.maintenance.ledger_summaries`/`rollup_sql` use for every
# fleet-wide maintenance read/write: this table has no per-tenant
# session to bind an ORM query to, and an explicit UPSERT is clearer
# than `session.merge()` about exactly which columns are touched.

_UPSERT_SQL = text(
    """
    INSERT INTO fleet_daily_scorecard (
        date, provider_bytes, railway_cpu_seconds, railway_ram_gb_hours,
        railway_egress_gb, valid_fresh_matches, attempts_per_valid_fresh,
        browser_share, proxied_share, queue_oldest_seconds_p95,
        persistence_lag_seconds_p95, missing_metric_fraction,
        budget_reserved_usd, budget_settled_usd, backup_egress_gb,
        cost_per_valid_fresh_micro_usd, updated_at
    ) VALUES (
        :date, :provider_bytes, :railway_cpu_seconds, :railway_ram_gb_hours,
        :railway_egress_gb, :valid_fresh_matches, :attempts_per_valid_fresh,
        :browser_share, :proxied_share, :queue_oldest_seconds_p95,
        :persistence_lag_seconds_p95, :missing_metric_fraction,
        :budget_reserved_usd, :budget_settled_usd, :backup_egress_gb,
        :cost_per_valid_fresh_micro_usd, now()
    )
    ON CONFLICT (date) DO UPDATE SET
        provider_bytes = EXCLUDED.provider_bytes,
        railway_cpu_seconds = EXCLUDED.railway_cpu_seconds,
        railway_ram_gb_hours = EXCLUDED.railway_ram_gb_hours,
        railway_egress_gb = EXCLUDED.railway_egress_gb,
        valid_fresh_matches = EXCLUDED.valid_fresh_matches,
        attempts_per_valid_fresh = EXCLUDED.attempts_per_valid_fresh,
        browser_share = EXCLUDED.browser_share,
        proxied_share = EXCLUDED.proxied_share,
        queue_oldest_seconds_p95 = EXCLUDED.queue_oldest_seconds_p95,
        persistence_lag_seconds_p95 = EXCLUDED.persistence_lag_seconds_p95,
        missing_metric_fraction = EXCLUDED.missing_metric_fraction,
        budget_reserved_usd = EXCLUDED.budget_reserved_usd,
        budget_settled_usd = EXCLUDED.budget_settled_usd,
        backup_egress_gb = EXCLUDED.backup_egress_gb,
        cost_per_valid_fresh_micro_usd = EXCLUDED.cost_per_valid_fresh_micro_usd,
        updated_at = now()
    """
)


def upsert_scorecard(session: Session, row: ScorecardRow) -> None:
    """Write ``row`` as day ``row.date``'s scorecard. Idempotent re-run
    (a re-computation of the same day overwrites, never duplicates or
    accumulates)."""
    session.execute(_UPSERT_SQL, row.as_dict_raw())  # noqa: workspace-scope


_READ_ONE_DAY_SQL = text(
    "SELECT * FROM fleet_daily_scorecard WHERE date = :date"
)

_READ_RANGE_SQL = text(
    """
    SELECT * FROM fleet_daily_scorecard
    WHERE date >= :since AND date <= :until
    ORDER BY date DESC
    """
)


def _row_to_scorecard(row: Any) -> ScorecardRow:
    return ScorecardRow(
        date=row.date,
        provider_bytes=row.provider_bytes,
        railway_cpu_seconds=row.railway_cpu_seconds,
        railway_ram_gb_hours=row.railway_ram_gb_hours,
        railway_egress_gb=row.railway_egress_gb,
        valid_fresh_matches=row.valid_fresh_matches,
        attempts_per_valid_fresh=row.attempts_per_valid_fresh,
        browser_share=row.browser_share,
        proxied_share=row.proxied_share,
        queue_oldest_seconds_p95=row.queue_oldest_seconds_p95,
        persistence_lag_seconds_p95=row.persistence_lag_seconds_p95,
        missing_metric_fraction=row.missing_metric_fraction,
        budget_reserved_usd=row.budget_reserved_usd,
        budget_settled_usd=row.budget_settled_usd,
        backup_egress_gb=row.backup_egress_gb,
        cost_per_valid_fresh_micro_usd=row.cost_per_valid_fresh_micro_usd,
    )


def read_scorecard(session: Session, day: date_type) -> ScorecardRow | None:
    """Read one day's already-written scorecard row, or ``None``."""
    row = session.execute(_READ_ONE_DAY_SQL, {"date": day}).first()  # noqa: workspace-scope
    return None if row is None else _row_to_scorecard(row)


def read_scorecard_range(
    session: Session, since: date_type, until: date_type
) -> list[ScorecardRow]:
    """Every written row in ``[since, until]`` inclusive, newest first."""
    rows = session.execute(  # noqa: workspace-scope
        _READ_RANGE_SQL, {"since": since, "until": until}
    ).all()
    return [_row_to_scorecard(row) for row in rows]


@dataclass(frozen=True)
class RunReport:
    """What one `MAINTENANCE_DAILY_SCORECARD` invocation did."""

    date: date_type
    row: ScorecardRow


def run_daily_scorecard(
    session: Session,
    *,
    day: date_type | None = None,
    now: datetime | None = None,
    redis_client: Any | None = None,
) -> RunReport:
    """Compute and durably write one day's scorecard.

    ``day`` defaults to YESTERDAY UTC (the most-recently-COMPLETED day)
    -- the same convention `app_shared.maintenance.rollups.run_daily_rollup`
    uses and for the same reason: TODAY is still accumulating rows, so a
    row written for it would be silently incomplete without ever saying
    so, whereas yesterday's window is closed and every query above is
    then an honest, stable answer for that calendar day.
    """
    now = now or datetime.now(timezone.utc)
    if day is None:
        day = (now - timedelta(days=1)).date()
    row = compute_scorecard(session, day, redis_client=redis_client)
    upsert_scorecard(session, row)
    return RunReport(date=day, row=row)
