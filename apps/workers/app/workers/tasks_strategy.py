"""Domain strategy optimizer Celery tasks (SPEC-12).

`STRATEGY_DISCOVERY_RUN` (`contracts/discovery.md`, D7, FR-016..FR-019,
US3) is the ONE path allowed to probe multiple **access** methods (the
internal ladder `DIRECT_HTTP -> DIRECT_HTTP_RETRY -> PROXY_HTTP`;
`PLAYWRIGHT_PROXY` is reserved vocabulary and is skipped/short-circuited
here until SPEC-14 can execute it, F2) then read off whichever
**extraction** method the reused `scrape_core.extraction.pipeline.extract`
chain hits first, on a small (3-10 URL) sample. Both the automatic
trigger (US2 `resolve_or_create_strategy_profile`, new key) and the
operator API (`apps/api/app/routers/strategy.py`) enqueue this exact task
with the same payload shape (spec Clarification #3) and converge on the
shared `app_shared.strategy.seed.seed_from_discovery` helper.

Why this task does its own direct/proxy HTTP fetch rather than
dispatching a Scrapyd spider run (research D7 "a Celery task ... can
legitimately walk multiple methods on a small sample"): `generic_price_
spider`/`ScrapydDispatchClient.schedule` is fire-and-forget-async (a
`jobid`, no synchronous per-URL result) and is driven by persisted
`competitor_product_matches`, not an ad-hoc probe sample -- there is no
existing synchronous "fetch one URL with method X" path to reuse
(Constitution V already forbids doing this probing *inside* the spider
itself: "spiders persist only"). This task reuses everything **around**
the fetch instead: `scrape_core.extraction.pipeline.extract` for
extraction, `scrape_core.validation.validate_candidate` for the
promotion-quality bar, `app_shared.url_safety.validate_competitor_url`
for the SSRF guard, and the existing `app_shared.access`
provider/assignment plumbing for `PROXY_HTTP`. Fully off-reactor (a
Celery task, never the Twisted reactor/Scrapy) -- blocking HTTP calls
here are safe and expected (Constitution V).
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.workers.celery_app import app
from app_shared.access.repository import visible_providers_select
from app_shared.config import Settings, get_settings
from app_shared.database import get_session, set_workspace_context
from app_shared.enums import (
    AccessMethod,
    DiscoveryRunStatus,
    ExtractionMethod,
    ProxyProviderStatus,
    StrategyStatus,
)
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.strategy import DomainStrategyProfile, StrategyDiscoveryRun
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.encryption import SecretDecryptionError, decrypt_secret
from app_shared.strategy.promotion import PromotionThresholds
from app_shared.strategy.repository import resolve_profile
from app_shared.strategy.seed import DiscoverySeedConfidences, seed_from_discovery, validate_sample_size
from app_shared.task_names import STRATEGY_DISCOVERY_RUN
from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

from scrape_core.extraction.pipeline import extract
from scrape_core.validation import Accepted, validate_candidate

logger = logging.getLogger(__name__)

#: `STRATEGY_DISCOVERY_RUN` runs on its own queue (data-model.md §8,
#: contracts/discovery.md), distinct from `maintenance`.
_DISCOVERY_QUEUE = "strategy_discovery"

#: Deterministic cost order (cheapest first, contracts/discovery.md
#: "Select winner") -- `PLAYWRIGHT_PROXY` is reserved vocabulary and is
#: never a probe candidate here (F2, until SPEC-14).
_ACCESS_LADDER: tuple[AccessMethod, ...] = (
    AccessMethod.DIRECT_HTTP,
    AccessMethod.DIRECT_HTTP_RETRY,
    AccessMethod.PROXY_HTTP,
)
_ACCESS_COST_ORDER: dict[AccessMethod, int] = {
    AccessMethod.DIRECT_HTTP: 0,
    AccessMethod.DIRECT_HTTP_RETRY: 1,
    AccessMethod.PROXY_HTTP: 2,
    AccessMethod.PLAYWRIGHT_PROXY: 3,
}

#: The spec §16 extraction escalation order (contracts/discovery.md
#: "Select winner": `PLATFORM_PATTERN, JSON_LD, EMBEDDED_JSON,
#: CSS_SELECTOR, XPATH, REGEX, PLAYWRIGHT_RENDERED_SELECTOR`), mapped to
#: this codebase's `ExtractionMethod` names (research D1).
#: `SINGLE_NUMBER` is `REGEX`'s internal fallback
#: (`scrape_core.extraction.regex`), placed just after it.
_EXTRACTION_COST_ORDER: dict[ExtractionMethod, int] = {
    ExtractionMethod.PLATFORM_JSON: 0,
    ExtractionMethod.JSON_LD: 1,
    ExtractionMethod.EMBEDDED_JSON: 2,
    ExtractionMethod.CSS: 3,
    ExtractionMethod.XPATH: 4,
    ExtractionMethod.REGEX: 5,
    ExtractionMethod.SINGLE_NUMBER: 6,
    ExtractionMethod.PLAYWRIGHT: 7,
}

#: Conservative per-request timeout for the small discovery sample. Not a
#: `Settings` knob (data-model §7's 10 SPEC-12 knobs are exhaustive) --
#: purely an implementation constant of this task's own probe loop.
_PROBE_TIMEOUT_SECONDS = 15.0


@dataclass
class _Tally:
    """Per-`(access, extraction)` combo running state across the sample."""

    qualifying_urls: set[str] = field(default_factory=set)
    confidence_sum: Decimal = Decimal("0")
    confidence_count: int = 0


# --- sample selection (AUTO trigger fallback) ------------------------------


def _select_sample_urls(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    url_pattern: str,
    max_sample: int,
) -> list[str]:
    """Auto-trigger fallback (contracts/discovery.md "Payload"): select up
    to `max_sample` matched URLs for this key from `competitor_product_matches`
    when the caller didn't supply `sample_urls` (US2's AUTO enqueue, empty list)."""
    stmt = (
        scoped_select(CompetitorProductMatch, workspace_id)
        .where(
            CompetitorProductMatch.competitor_id == competitor_id,
            CompetitorProductMatch.url_pattern == url_pattern,
        )
        .limit(max_sample)
    )
    return [row.competitor_url for row in session.execute(stmt).scalars().all()]


# --- profile get-or-create (no enqueue -- see seed.py docstring) ----------


def _get_or_create_profile(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    domain: str,
    url_pattern: str,
) -> DomainStrategyProfile:
    """Get-or-create the profile for this run's key, deliberately WITHOUT
    the enqueue side effect `resolve_or_create_strategy_profile` carries
    (US2) -- a discovery task seeding its own key must never re-trigger
    its own discovery."""
    profile = resolve_profile(session, workspace_id, competitor_id, domain, url_pattern)
    if profile is not None:
        return profile

    candidate = DomainStrategyProfile(
        workspace_id=workspace_id,
        competitor_id=competitor_id,
        domain=domain,
        url_pattern=url_pattern,
        url_pattern_version=URL_PATTERN_ALGORITHM_VERSION,
        status=StrategyStatus.DISCOVERY_REQUIRED,
    )
    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
    except IntegrityError:
        existing = resolve_profile(session, workspace_id, competitor_id, domain, url_pattern)
        if existing is None:
            raise
        return existing
    return candidate


# --- probing ----------------------------------------------------------------


def _fetch_direct(url: str, *, retry: bool) -> str | None:
    """One `DIRECT_HTTP` (or `DIRECT_HTTP_RETRY`) fetch attempt. `None` on
    any failure/non-2xx -- never raises (the caller just records "no
    qualifying observation" for this combo/url, contracts/discovery.md)."""
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=_PROBE_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.info(
                "strategy_discovery: direct fetch failed url=%s attempt=%d error=%s",
                url,
                attempt,
                exc,
            )
            continue
        if response.ok:
            return response.text
    return None


def _build_proxy_kwargs(session: Session, workspace_id: uuid.UUID) -> dict[str, object] | None:
    """Build `requests.get(...)` kwargs for one `PROXY_HTTP` attempt from
    the first visible, `ACTIVE` proxy provider (own or global,
    `app_shared.access.repository.visible_providers_select`, reused --
    no new provider-selection logic). `None` when no provider is
    configured/visible for this workspace -- `PROXY_HTTP` is then simply
    not a discovery candidate (never an error).

    Mirrors `generic_price_spider`'s existing provider -> proxy meta +
    `Proxy-Authorization` header construction (`_parse_host_port` +
    `decrypt_secret`), simplified to a one-off probe: no rotation/
    stickiness policy applies to a single discovery sample.
    """
    providers = session.execute(visible_providers_select(workspace_id)).scalars().all()
    active = [p for p in providers if p.status == ProxyProviderStatus.ACTIVE]
    if not active:
        return None

    provider = active[0]
    proxy_url = provider.base_url if "://" in provider.base_url else f"http://{provider.base_url}"

    headers: dict[str, str] = {}
    if provider.username and provider.password_encrypted and provider.password_key_version:
        try:
            password = decrypt_secret(provider.password_encrypted, provider.password_key_version)
        except SecretDecryptionError as exc:
            logger.warning(
                "strategy_discovery: proxy password decryption failed provider_id=%s error=%s",
                provider.id,
                exc,
            )
            return {"proxies": {"http": proxy_url, "https": proxy_url}}
        token = base64.b64encode(f"{provider.username}:{password}".encode()).decode()
        headers["Proxy-Authorization"] = f"Basic {token}"

    return {"proxies": {"http": proxy_url, "https": proxy_url}, "headers": headers}


def _fetch_via_proxy(session: Session, workspace_id: uuid.UUID, url: str) -> str | None:
    """One `PROXY_HTTP` fetch attempt; `None` when no provider is
    configured or the request fails -- never raises."""
    proxy_kwargs = _build_proxy_kwargs(session, workspace_id)
    if proxy_kwargs is None:
        return None
    try:
        response = requests.get(url, timeout=_PROBE_TIMEOUT_SECONDS, **proxy_kwargs)
    except requests.RequestException as exc:
        logger.info("strategy_discovery: proxy fetch failed url=%s error=%s", url, exc)
        return None
    return response.text if response.ok else None


def _fetch(session: Session, workspace_id: uuid.UUID, access_method: AccessMethod, url: str) -> str | None:
    if access_method is AccessMethod.DIRECT_HTTP:
        return _fetch_direct(url, retry=False)
    if access_method is AccessMethod.DIRECT_HTTP_RETRY:
        return _fetch_direct(url, retry=True)
    if access_method is AccessMethod.PROXY_HTTP:
        return _fetch_via_proxy(session, workspace_id, url)
    # PLAYWRIGHT_PROXY is never in `_ACCESS_LADDER` -- unreachable (F2).
    raise AssertionError(f"unexpected access method probed: {access_method!r}")  # pragma: no cover


def _probe_sample(
    session: Session, *, workspace_id: uuid.UUID, urls: list[str], thresholds: PromotionThresholds
) -> dict[tuple[AccessMethod, ExtractionMethod], _Tally]:
    """Drive `urls` through each candidate access method, then the reused
    extraction chain, tallying qualifying observations per `(access,
    extraction)` combo (contracts/discovery.md steps 3-4)."""
    confidence_cfg = resolve_confidence_rules(
        {"min_accepted_confidence": float(thresholds.confidence_threshold)}
    )
    tallies: dict[tuple[AccessMethod, ExtractionMethod], _Tally] = {}

    for access_method in _ACCESS_LADDER:
        for url in urls:
            html = _fetch(session, workspace_id, access_method, url)
            if html is None:
                continue

            candidate = extract(html)
            if candidate is None:
                continue

            outcome = validate_candidate(candidate, {}, confidence_cfg)
            if not isinstance(outcome, Accepted):
                continue

            key = (access_method, candidate.method)
            tally = tallies.setdefault(key, _Tally())
            tally.qualifying_urls.add(url)
            tally.confidence_sum += Decimal(str(candidate.confidence))
            tally.confidence_count += 1

    return tallies


def select_discovery_winner(
    tallies: dict[tuple[AccessMethod, ExtractionMethod], _Tally],
) -> tuple[AccessMethod, ExtractionMethod, DiscoverySeedConfidences] | None:
    """Pick the `(access, extraction)` combo with the most qualifying
    sample URLs; ties broken by cheapest access then earliest extraction
    order (contracts/discovery.md "Select winner"). `None` = `NO_WINNER`
    (no combo had any qualifying observation, US3 AS4).

    Both access and extraction confidence/qualifying-count feed the same
    tally -- a discovery probe's qualifying observations always used the
    winning `(access, extraction)` pair together, unlike US1's
    independently-learned access/extraction promotion (US1 AS5).
    """
    qualifying = {combo: tally for combo, tally in tallies.items() if tally.qualifying_urls}
    if not qualifying:
        return None

    def _sort_key(combo: tuple[AccessMethod, ExtractionMethod]) -> tuple[int, int, int]:
        access_method, extraction_method = combo
        return (
            -len(qualifying[combo].qualifying_urls),
            _ACCESS_COST_ORDER[access_method],
            _EXTRACTION_COST_ORDER.get(extraction_method, len(_EXTRACTION_COST_ORDER)),
        )

    winning_combo = min(qualifying, key=_sort_key)
    access_method, extraction_method = winning_combo
    tally = qualifying[winning_combo]

    confidence = (
        (tally.confidence_sum / tally.confidence_count) if tally.confidence_count else None
    )
    qualifying_count = len(tally.qualifying_urls)
    confidences = DiscoverySeedConfidences(
        access_confidence=confidence,
        access_qualifying_count=qualifying_count,
        access_distinct_url_count=qualifying_count,
        extraction_confidence=confidence,
        extraction_qualifying_count=qualifying_count,
        extraction_distinct_url_count=qualifying_count,
    )
    return access_method, extraction_method, confidences


def _promotion_thresholds(settings: Settings) -> PromotionThresholds:
    return PromotionThresholds(
        min_successes=settings.STRATEGY_PROMOTION_MIN_SUCCESSES,
        min_distinct_urls=settings.STRATEGY_PROMOTION_MIN_DISTINCT_URLS,
        confidence_threshold=Decimal(str(settings.STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD)),
    )


def _fail_run(session: Session, run: StrategyDiscoveryRun) -> None:
    run.status = DiscoveryRunStatus.FAILED
    run.completed_at = datetime.now(timezone.utc)
    session.commit()


@app.task(name=STRATEGY_DISCOVERY_RUN)
def run_discovery(
    workspace_id: str,
    competitor_id: str,
    domain: str,
    url_pattern: str,
    sample_urls: list[str] | None = None,
    triggered_by: str = "AUTO",
    run_id: str | None = None,
) -> None:
    """`STRATEGY_DISCOVERY_RUN` (`strategy_discovery` queue,
    contracts/discovery.md, D7, FR-016..FR-019, US3).

    `run_id` is the (optional) already-created `PENDING` row -- the
    operator API (`POST /v1/strategy/discovery-runs`) creates its run
    synchronously so it has something to return in its 202 response,
    then passes `run_id` here; the AUTO trigger (US2
    `resolve_or_create_strategy_profile`) has no such row yet and leaves
    `run_id=None`, so this task creates it. Both converge on the same
    lifecycle from here (contracts/discovery.md "Lifecycle").
    """
    settings = get_settings()
    ws = uuid.UUID(str(workspace_id))
    comp_id = uuid.UUID(str(competitor_id))
    thresholds = _promotion_thresholds(settings)

    with get_session() as session:
        set_workspace_context(session, ws)

        run: StrategyDiscoveryRun | None = None
        if run_id is not None:
            run = scoped_get(session, StrategyDiscoveryRun, uuid.UUID(str(run_id)), ws)

        urls = list(sample_urls or [])
        if not urls:
            urls = _select_sample_urls(
                session,
                workspace_id=ws,
                competitor_id=comp_id,
                url_pattern=url_pattern,
                max_sample=settings.STRATEGY_DISCOVERY_MAX_SAMPLE,
            )

        size_ok = validate_sample_size(
            len(urls),
            min_sample=settings.STRATEGY_DISCOVERY_MIN_SAMPLE,
            max_sample=settings.STRATEGY_DISCOVERY_MAX_SAMPLE,
        )

        if run is None:
            run = StrategyDiscoveryRun(
                workspace_id=ws,
                competitor_id=comp_id,
                domain=domain,
                url_pattern=url_pattern,
                sample_size=len(urls),
                status=DiscoveryRunStatus.PENDING,
            )
            session.add(run)
            session.flush()

        if not size_ok:
            # FR-019, US3 AS2 -- out-of-bounds sample (the AUTO path can
            # land here when too few matches exist yet for this key; the
            # operator path already rejects at the API with a 422 before
            # ever enqueuing, contracts/discovery.md step 1).
            _fail_run(session, run)
            return

        run.sample_size = len(urls)
        run.status = DiscoveryRunStatus.RUNNING
        session.flush()

        safe_urls: list[str] = []
        for url in urls:
            try:
                validate_competitor_url(url)
            except UnsafeUrlError as exc:
                logger.warning("strategy_discovery: unsafe sample url=%s reason=%s", url, exc)
                continue
            safe_urls.append(url)

        if not safe_urls:
            _fail_run(session, run)
            return

        try:
            tallies = _probe_sample(
                session, workspace_id=ws, urls=safe_urls, thresholds=thresholds
            )
            winner = select_discovery_winner(tallies)
        except Exception:
            logger.exception("strategy_discovery: probe failed run_id=%s", run.id)
            _fail_run(session, run)
            return

        profile = _get_or_create_profile(
            session, workspace_id=ws, competitor_id=comp_id, domain=domain, url_pattern=url_pattern
        )
        now = datetime.now(timezone.utc)

        if winner is None:
            run.status = DiscoveryRunStatus.NO_WINNER
            run.completed_at = now
            seed_from_discovery(
                profile,
                winning_access=None,
                winning_extraction=None,
                confidences=None,
                thresholds=thresholds,
            )
            session.commit()
            logger.info(
                "strategy_discovery_completed run_id=%s status=NO_WINNER sample_size=%d",
                run.id,
                run.sample_size,
            )
            return

        access_method, extraction_method, confidences = winner
        run.winning_access_method = access_method
        run.winning_extraction_method = extraction_method
        run.status = DiscoveryRunStatus.COMPLETED
        run.completed_at = now
        seed_from_discovery(
            profile,
            winning_access=access_method,
            winning_extraction=extraction_method,
            confidences=confidences,
            thresholds=thresholds,
        )
        session.commit()
        logger.info(
            "strategy_discovery_completed run_id=%s status=COMPLETED "
            "winning_access=%s winning_extraction=%s sample_size=%d triggered_by=%s",
            run.id,
            access_method,
            extraction_method,
            run.sample_size,
            triggered_by,
        )
