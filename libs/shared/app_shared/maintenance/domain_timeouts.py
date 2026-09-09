"""Per-domain request timeouts learned from success latency (EPA C1/F08).

WHY THIS EXISTS
---------------
The deep dive measured a **46.9 s average** on proxied-HTTP attempts.
That number is not a latency measurement; it is a *timeout* measurement.
One global timeout (``SCRAPE_DOWNLOAD_TIMEOUT_SECONDS``, 60 s) is applied
to every domain, so every domain that is going to fail costs the full 60
seconds of a worker slot, a fleet lease and — on the proxied path — real
egress, before anyone learns anything. Meanwhile a domain that answers
successfully in 1.2 s at p95 gains nothing whatsoever from being allowed
60.

The fix is to let each domain's own successful attempts set its ceiling:

    ``timeout = clamp(1.5 x p95(successful attempt duration, 7 d), 10 s, 60 s)``

Three deliberate properties:

* **Successful attempts only.** Failures are dominated by the current
  timeout, so including them would make the new timeout a function of
  the old one and the value would ratchet upward forever. Successes are
  the only sample that describes what the domain actually needs.
* **p95, times 1.5.** p95 is what a healthy fetch costs on a bad day;
  the 1.5x headroom is what keeps the tuner from clipping the tail it
  just measured. The pair is chosen so a domain whose latency doubles
  overnight still completes rather than being cut off by yesterday's
  number.
* **clamp(10 s, 60 s).** The floor stops a fast domain from getting a
  timeout so tight that one slow day looks like an outage; the ceiling
  is the existing global default, so this task can only ever make things
  faster than today, never slower. A domain with too few successes to
  measure is left alone entirely — ``NULL`` keeps meaning "use the
  setting", never "unlimited" and never "zero".

WHAT IT WRITES
--------------
``domain_rules.request_timeout_seconds`` only, on the FLEET-global
``domain_rules`` table (no ``workspace_id``, no RLS — a domain's timeout
is a property of the domain, exactly like its fleet ceiling). Rows are
inserted for domains that have none. Nothing else on the row is ever
touched: an operator's ``fleet_concurrency`` / ``fleet_rate_per_minute``
/ ``notes`` are theirs.

Pure-ish by design: :func:`clamp_domain_timeout` and
:func:`plan_domain_timeouts` are total functions over already-fetched
numbers and are where the policy lives;
:func:`tune_domain_timeouts` is the thin I/O shell the Celery task
(``MAINTENANCE_DOMAIN_TIMEOUT_TUNE``) calls. Scraping-free (Constitution
I/V): stdlib + SQLAlchemy + ``app_shared`` only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "DOMAIN_TIMEOUT_HEADROOM",
    "DOMAIN_TIMEOUT_MAX_SECONDS",
    "DOMAIN_TIMEOUT_MIN_SAMPLE",
    "DOMAIN_TIMEOUT_MIN_SECONDS",
    "DOMAIN_TIMEOUT_WINDOW_DAYS",
    "SUCCESS_LATENCY_P95_SQL",
    "DomainTimeoutPlan",
    "clamp_domain_timeout",
    "plan_domain_timeouts",
    "tune_domain_timeouts",
]

logger = logging.getLogger(__name__)

#: Multiplier applied to the measured p95 before clamping. Headroom, not
#: a fudge factor: p95 is a *sample* of a distribution whose tail we
#: still want to complete.
DOMAIN_TIMEOUT_HEADROOM = 1.5
#: Floor. Below this a single bad minute on a normally-fast domain would
#: read as an outage and cost a whole refresh.
DOMAIN_TIMEOUT_MIN_SECONDS = 10
#: Ceiling — the existing global ``SCRAPE_DOWNLOAD_TIMEOUT_SECONDS``
#: default. This task can only ever tighten a domain, never loosen it
#: past what it already had.
DOMAIN_TIMEOUT_MAX_SECONDS = 60
#: Lookback for the p95.
DOMAIN_TIMEOUT_WINDOW_DAYS = 7
#: Successful attempts a domain needs in the window before its p95 is
#: believed. Under this the domain is SKIPPED (left at the setting), not
#: given a floor value: an invented timeout is worse than the default.
DOMAIN_TIMEOUT_MIN_SAMPLE = 20


#: p95 of *successful* attempt duration per domain over the window.
#:
#: The domain comes from ``competitors.domain`` via the attempt's match —
#: never parsed out of ``request_attempts.url`` — so it is byte-identical
#: to what ``domain_rules.domain`` and ``domain_playbooks.domain`` store
#: and a redirect to another host can never file latency under the wrong
#: domain. ``origin = 'SCRAPE'`` excludes discovery probes: the probe
#: ladder deliberately tries transports that do not work, and its
#: latency describes the probe, not the domain.
SUCCESS_LATENCY_P95_SQL = """
SELECT
    c.domain AS domain,
    count(*) AS sample_size,
    percentile_cont(0.95) WITHIN GROUP (
        ORDER BY ra.response_time_ms
    ) AS p95_ms
FROM request_attempts AS ra
JOIN competitor_product_matches AS m ON m.id = ra.match_id
JOIN competitors AS c ON c.id = m.competitor_id
WHERE ra.created_at > now() - make_interval(days => :window_days)
  AND ra.success IS TRUE
  AND ra.origin = 'SCRAPE'
  AND ra.response_time_ms IS NOT NULL
GROUP BY c.domain
"""


@dataclass(frozen=True)
class DomainTimeoutPlan:
    """One domain's decision, computed before anything is written."""

    domain: str
    sample_size: int
    p95_seconds: float
    timeout_seconds: int
    #: What the row already had, or ``None`` when it had no override.
    current_seconds: int | None = None

    @property
    def changed(self) -> bool:
        return self.timeout_seconds != self.current_seconds


def clamp_domain_timeout(p95_seconds: float) -> int:
    """``clamp(1.5 x p95, 10 s, 60 s)``, rounded up to whole seconds.

    Rounds UP (``ceil``) rather than to nearest: a timeout is a ceiling,
    and rounding a 10.2 s need down to 10 s would cut off the very
    requests the headroom was bought for. Non-finite or non-positive
    input is refused by the caller, never silently floored here.
    """
    from math import ceil

    scaled = ceil(p95_seconds * DOMAIN_TIMEOUT_HEADROOM)
    return max(DOMAIN_TIMEOUT_MIN_SECONDS, min(DOMAIN_TIMEOUT_MAX_SECONDS, scaled))


def plan_domain_timeouts(
    rows: Iterable[Any],
    *,
    current: Mapping[str, int | None] | None = None,
    min_sample: int = DOMAIN_TIMEOUT_MIN_SAMPLE,
) -> list[DomainTimeoutPlan]:
    """Turn ``(domain, sample_size, p95_ms)`` rows into decisions.

    Pure — no I/O, no clock — so the whole policy is unit-testable
    against plain tuples. A domain is SKIPPED (absent from the result)
    when it has fewer than ``min_sample`` successes in the window, or a
    ``NULL``/non-positive p95: those are the cases where we do not know
    what the domain needs, and ``NULL`` in ``domain_rules`` already means
    exactly that.
    """
    current = current or {}
    plans: list[DomainTimeoutPlan] = []
    for row in rows:
        domain = getattr(row, "domain", None) if not isinstance(row, (tuple, list)) else row[0]
        sample_size = (
            getattr(row, "sample_size", None) if not isinstance(row, (tuple, list)) else row[1]
        )
        p95_ms = getattr(row, "p95_ms", None) if not isinstance(row, (tuple, list)) else row[2]
        if not domain:
            continue
        sample_size = int(sample_size or 0)
        if sample_size < min_sample:
            logger.debug(
                "domain_timeouts: %s skipped -- %d successes < %d",
                domain,
                sample_size,
                min_sample,
            )
            continue
        if p95_ms is None:
            continue
        p95_seconds = float(p95_ms) / 1000.0
        if p95_seconds <= 0:
            continue
        plans.append(
            DomainTimeoutPlan(
                domain=str(domain),
                sample_size=sample_size,
                p95_seconds=p95_seconds,
                timeout_seconds=clamp_domain_timeout(p95_seconds),
                current_seconds=current.get(str(domain)),
            )
        )
    return plans


def tune_domain_timeouts(session: Any, *, window_days: int = DOMAIN_TIMEOUT_WINDOW_DAYS) -> list[DomainTimeoutPlan]:
    """Compute and persist every domain's timeout. Returns what changed.

    ``session`` must be the BYPASSRLS system session: ``domain_rules`` is
    global and the p95 is a FLEET aggregate — one workspace's view of a
    domain's latency is not the fleet's.

    Writes ``request_timeout_seconds`` only, and only when the value
    actually differs, so a steady-state run writes nothing. Upserts on
    ``domain`` (the table's unique key) so a domain with no row yet gets
    one carrying nothing but its timeout — every other column stays
    ``NULL``, i.e. "use the setting".
    """
    from sqlalchemy import func as sa_func, text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app_shared.models.domain_rules import DomainRule

    rows = session.execute(
        text(SUCCESS_LATENCY_P95_SQL), {"window_days": int(window_days)}
    ).all()
    existing = {
        row.domain: row.request_timeout_seconds
        for row in session.execute(
            text("SELECT domain, request_timeout_seconds FROM domain_rules")
        ).all()
    }
    plans = plan_domain_timeouts(rows, current=existing)

    written: list[DomainTimeoutPlan] = []
    for plan in plans:
        if not plan.changed:
            continue
        statement = (
            pg_insert(DomainRule.__table__)
            .values(domain=plan.domain, request_timeout_seconds=plan.timeout_seconds)
            .on_conflict_do_update(
                index_elements=["domain"],
                # `updated_at` is set explicitly: SQLAlchemy's `onupdate`
                # fires for ORM/Core UPDATEs, never for an ON CONFLICT DO
                # UPDATE clause, and a row whose timeout changed while
                # `updated_at` stood still is a lie an operator will read.
                set_={
                    "request_timeout_seconds": plan.timeout_seconds,
                    "updated_at": sa_func.now(),
                },
            )
        )
        session.execute(statement)
        written.append(plan)
        logger.info(
            "domain_timeouts: %s timeout %ss -> %ss (p95=%.3fs over %d successes)",
            plan.domain,
            plan.current_seconds,
            plan.timeout_seconds,
            plan.p95_seconds,
            plan.sample_size,
        )
    if not written:
        logger.info(
            "domain_timeouts: %d domains measured, none changed", len(plans)
        )
    return written
