"""`GET /live`, `GET /health/scraping` — F16 liveness/dependency/scraping split.

EPA B8 splits health signal into three tiers, each answering a different
question and consumed by a different actor:

* `GET /live` (this module) — process liveness. Never touches a
  dependency, same discipline the pre-existing `GET /health`
  (`apps.api.app.main`, SPEC-01/FR-020) already holds to. This is that
  same contract, restated under the F16 name; the original `/health`
  stays mounted unchanged for callers that already depend on it.
* `GET /ready` (`apps.api.app.routers.ready`) — is this instance fit to
  receive traffic: database, Redis, migration head, and required
  heartbeats. An orchestrator acts on this one (200 routes traffic in,
  503 does not).
* `GET /health/scraping` (this module) — is the SCRAPING PIPELINE
  healthy, as opposed to the API process. Breaker evaluation staleness,
  24h price-freshness, and the oldest PENDING scrape target. This is a
  product-quality signal, not an infrastructure-dependency signal:
  `/ready` staying healthy while scraping is degraded is not a
  contradiction — it is the accurate report of two different things
  being asked. **This endpoint NEVER gates `/ready`** — a degraded
  scraping pipeline does not mean the API process itself is unfit to
  serve requests (auth, product CRUD, etc. keep working), so folding it
  into `/ready` would take a healthy API instance out of rotation for a
  problem an orchestrator restarting instances cannot fix.

Unauthenticated, like `/health`, `/ready` and `/version`: these are
platform/operator probes, not tenant or admin surfaces, and (matching
`/ready`'s own discipline) leak nothing sensitive — only booleans,
counts, ages, and an exception CLASS NAME, never a message.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app_shared.config import get_settings
from app_shared.database import get_session
from app_shared.enums import MatchStatus, ScrapeTargetStatus
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJobTarget
from app_shared.models.observations import PriceObservation
from app_shared.models.proxy_breaker import GLOBAL_BREAKER_SCOPE, ProxyCircuitBreaker

router = APIRouter(tags=["health"])

#: Window the freshness fraction is measured over. 24h matches the
#: `freshness_fraction_24h` metric name this endpoint and
#: `app_shared.opsmetrics.rules` (EPA B9) both use.
_FRESHNESS_WINDOW_HOURS = 24

#: Below this fraction of ACTIVE matches having a successful price
#: observation in the last 24h, the scraping pipeline is reported
#: `degraded` — same threshold `app_shared.opsmetrics.rules` (EPA B9)
#: evaluates as `freshness_fraction_24h < 0.95`, so an operator reading
#: either surface sees the same verdict.
_DEGRADED_FRESHNESS_FRACTION = 0.95


class ScrapingHealthResponse(BaseModel):
    #: `"ok"` or `"degraded"` — never a third value; this endpoint never
    #: 5xxs on a degraded pipeline (unlike `/ready`), because it never
    #: gates anything an orchestrator acts on.
    status: str
    breaker_seconds_since_evaluation: float | None = None
    breaker_state: str | None = None
    #: Fraction of ACTIVE `competitor_product_matches` with a successful
    #: `price_observations` row in the last 24h. `0.0` (never `null`) when
    #: there are zero ACTIVE matches to freshen — "nothing is fresh" is
    #: the accurate report of that state, not a missing signal.
    freshness_fraction_24h: float = 0.0
    oldest_pending_target_age_seconds: float | None = None
    #: Set only when the underlying probe itself failed (an exception
    #: CLASS NAME, never a message — same discipline as `/ready`).
    detail: str | None = None


@router.get("/live")
def live() -> dict[str, str]:
    """Process-liveness probe (F16). Returns 200 whenever the process is
    serving — never touches the database, Redis, or Scrapyd, same
    contract as the pre-existing `GET /health` (`apps.api.app.main`,
    SPEC-01/FR-020)."""
    return {"status": "ok"}


def _breaker_enabled() -> bool:
    """`PROXY_BREAKER_ENABLED`, without requiring a COMPLETE `Settings`.

    Same H2 lesson `apps.api.app.routers.ready` applies one module over:
    a probe that constructs the whole settings object inherits every
    unrelated required field, and a process missing one then reports a
    `ValidationError` as a failed dependency — a config problem
    masquerading as an outage. Defaulting to "enabled" on any failure is
    the fail-safe side of the question.
    """
    try:
        return bool(get_settings().PROXY_BREAKER_ENABLED)
    except Exception:  # noqa: BLE001 - a probe must not depend on full config
        return True


def _breaker_age(session: Session) -> tuple[float | None, str | None]:
    """Age (seconds) of the proxy breaker's last evaluation, and its
    state — moved here from `apps.api.app.routers.ready` (EPA B8/F16):
    breaker posture is a scraping-quality signal, not a readiness gate.

    `(None, None)` means "no signal" (breaker disabled, or never
    evaluated) — a labelled absence, not a fabricated healthy/unhealthy
    value.
    """
    if not _breaker_enabled():
        return None, None
    row = session.execute(
        select(ProxyCircuitBreaker.evaluated_at, ProxyCircuitBreaker.state).where(
            ProxyCircuitBreaker.scope_key == GLOBAL_BREAKER_SCOPE
        )
    ).first()
    if row is None:
        return None, None
    evaluated_at, state = row[0], row[1]
    # TIMESTAMPTZ everywhere, so a naive value can only come from outside
    # the ORM; assume UTC rather than crash the probe.
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - evaluated_at).total_seconds()
    return age_seconds, getattr(state, "value", state)


def _freshness_fraction_24h(session: Session, *, now: datetime) -> float:
    """Fraction of ACTIVE matches with a successful price observation in
    the last `_FRESHNESS_WINDOW_HOURS` hours. `0.0` (never `None`) when
    there are zero ACTIVE matches — see `ScrapingHealthResponse`."""
    since = now - timedelta(hours=_FRESHNESS_WINDOW_HOURS)
    fresh_exists = (
        select(PriceObservation.match_id)
        .where(
            PriceObservation.match_id == CompetitorProductMatch.id,
            PriceObservation.success.is_(True),
            PriceObservation.scraped_at >= since,
        )
        .exists()
    )
    total, fresh = session.execute(
        select(func.count(), func.count().filter(fresh_exists))
        .select_from(CompetitorProductMatch)
        .where(CompetitorProductMatch.status == MatchStatus.ACTIVE)
    ).one()
    total = int(total or 0)
    fresh = int(fresh or 0)
    if total == 0:
        return 0.0
    return fresh / total


def _oldest_pending_target_age_seconds(session: Session, *, now: datetime) -> float | None:
    """Age (seconds) of the oldest PENDING `scrape_job_targets` row, or
    `None` when the queue is empty — the same "labelled absence, not a
    fabricated 0" convention every other probe in this router uses."""
    oldest = session.execute(
        select(func.min(ScrapeJobTarget.created_at)).where(
            ScrapeJobTarget.status == ScrapeTargetStatus.PENDING
        )
    ).scalar_one_or_none()
    if oldest is None:
        return None
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return (now - oldest).total_seconds()


@router.get("/health/scraping", response_model=ScrapingHealthResponse)
def health_scraping() -> ScrapingHealthResponse:
    """Scraping-pipeline signal (EPA B8/F16). NEVER gates `/ready` — see
    this module's docstring. Always answers 200; `status` carries the
    verdict."""
    now = datetime.now(UTC)
    try:
        with get_session() as session:
            breaker_age, breaker_state = _breaker_age(session)
            freshness = _freshness_fraction_24h(session, now=now)
            oldest_pending = _oldest_pending_target_age_seconds(session, now=now)
    except Exception as exc:  # noqa: BLE001 - class name only, never the message
        return ScrapingHealthResponse(status="degraded", detail=exc.__class__.__name__)

    degraded = freshness < _DEGRADED_FRESHNESS_FRACTION
    return ScrapingHealthResponse(
        status="degraded" if degraded else "ok",
        breaker_seconds_since_evaluation=breaker_age,
        breaker_state=breaker_state,
        freshness_fraction_24h=freshness,
        oldest_pending_target_age_seconds=oldest_pending,
    )


__all__ = ["router"]
