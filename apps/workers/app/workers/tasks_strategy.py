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
from typing import Any

import requests
from sqlalchemy import select
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
    MethodType,
    ProxyProviderStatus,
    StrategyStatus,
)
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.strategy import DomainStrategyProfile, StrategyDiscoveryRun
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.redis_client import get_redis_client
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.encryption import SecretDecryptionError, decrypt_secret
from app_shared.strategy.flush import StrategyTransition, flush_profile
from app_shared.strategy.promotion import PromotionThresholds
from app_shared.strategy.rediscovery import (
    CombinedStats,
    RediscoveryThresholds,
    apply_rediscovery,
    build_recent_signals,
    evaluate_rediscovery,
)
from app_shared.strategy.repository import resolve_profile, stats_for_profile
from app_shared.strategy.seed import DiscoverySeedConfidences, seed_from_discovery, validate_sample_size
from app_shared.messaging import enqueue
from app_shared.strategy.stats_buffer import dirty_key, read_pending
from app_shared.task_names import (
    CREATE_WEBHOOK_EVENT,
    STRATEGY_DISCOVERY_RUN,
    STRATEGY_LIGHT_RECHECK,
    STRATEGY_PATTERN_BACKFILL,
    STRATEGY_STATS_FLUSH,
)
from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION, derive_url_pattern
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url
from app_shared.webhooks.payloads import build_strategy_event

from scrape_core.extraction.pipeline import extract
from scrape_core.validation import Accepted, validate_candidate

logger = logging.getLogger(__name__)

#: `STRATEGY_DISCOVERY_RUN` runs on its own queue (data-model.md §8,
#: contracts/discovery.md), distinct from `maintenance`.
_DISCOVERY_QUEUE = "strategy_discovery"

#: `STRATEGY_LIGHT_RECHECK` batch size per invocation (contracts/rediscovery.md
#: "Periodic light re-check", FR-021) -- a local implementation constant
#: (data-model §7's 10 SPEC-12 `Settings` knobs are exhaustive, T004), the
#: same precedent as this module's own `_PROBE_TIMEOUT_SECONDS`.
_LIGHT_RECHECK_BATCH_SIZE = 200

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


# --- periodic light re-check (US4, contracts/rediscovery.md, FR-021) ------


def _rediscovery_thresholds(settings: Settings) -> RediscoveryThresholds:
    return RediscoveryThresholds(
        consecutive_failures=settings.STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES,
        success_rate_floor=Decimal(str(settings.STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR)),
        low_confidence=Decimal(str(settings.STRATEGY_REDISCOVERY_LOW_CONFIDENCE)),
    )


#: Scale factor `stats_buffer.record_attempt` multiplies confidence by
#: before `HINCRBY conf_sum` (mirrors `stats_buffer._CONFIDENCE_SCALE` --
#: duplicated here, a plain int constant, rather than importing a private
#: name across the module boundary) -- needed to unscale a pending
#: `conf_sum` delta back into the same `Decimal` units as the persisted
#: `avg_confidence` column.
_CONFIDENCE_SCALE = 10_000


def _combined_stats_for_profile(
    session: Session, redis: Any, profile: DomainStrategyProfile
) -> CombinedStats:
    """Assemble `CombinedStats` (conditions 1-2, FR-020a(a)) from the
    profile's own `recent_failure_count` plus persisted
    `strategy_attempt_stats` **plus non-destructive pending buffered
    deltas** (`stats_buffer.read_pending`, FR-024) for whichever of its
    preferred access/extraction methods are set -- the worse (lower) of
    the two *combined* `success_rate`s is used so degradation on *either*
    learned channel is caught, whether or not a flush has run yet since
    the last few attempts. This periodic path only ever reads the pending
    buffer (`read_pending`) -- it never drains; draining is the flush
    task's job alone (contracts/rediscovery.md "Call sites").
    """
    rows = stats_for_profile(session, profile.workspace_id, profile.id)
    by_key = {(row.method_type, row.method_name): row for row in rows}

    success_rate: Decimal | None = None
    avg_confidence: Decimal | None = None
    for method_type, method_name in (
        (MethodType.ACCESS, profile.preferred_access_method),
        (MethodType.EXTRACTION, profile.preferred_extraction_method),
    ):
        if method_name is None:
            continue
        row = by_key.get((method_type, method_name))
        pending = read_pending(
            redis, profile_id=profile.id, method_type=method_type, method_name=method_name
        )

        persisted_attempt = row.attempt_count if row is not None else 0
        persisted_success = row.success_count if row is not None else 0
        combined_attempt = persisted_attempt + pending.attempt
        combined_success = persisted_success + pending.success
        if combined_attempt == 0:
            continue

        method_success_rate = Decimal(combined_success) / Decimal(combined_attempt)
        if success_rate is None or method_success_rate < success_rate:
            success_rate = method_success_rate

        if method_type is MethodType.EXTRACTION:
            persisted_conf_scaled = (
                (row.avg_confidence or Decimal("0")) * persisted_success * _CONFIDENCE_SCALE
                if row is not None
                else Decimal("0")
            )
            combined_conf_scaled = persisted_conf_scaled + Decimal(pending.conf_sum)
            avg_confidence = (
                combined_conf_scaled / _CONFIDENCE_SCALE / combined_success
                if combined_success
                else (row.avg_confidence if row is not None else None)
            )

    return CombinedStats(
        recent_failure_count=profile.recent_failure_count,
        success_rate=success_rate,
        avg_confidence=avg_confidence,
    )


def _scan_active_profile_refs(
    session: Session, *, limit: int
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Resolve `(id, workspace_id)` pairs for `ACTIVE` profiles, unscoped --
    the `_scan_job_refs` precedent (`tasks_jobs.py`): this maintenance task
    legitimately patrols every workspace, and every row is only read/
    mutated after `set_workspace_context` scopes it to its own workspace
    below (never cross-workspace data exposure)."""
    stmt = (
        select(DomainStrategyProfile.id, DomainStrategyProfile.workspace_id)  # noqa: workspace-scope
        .where(DomainStrategyProfile.status == StrategyStatus.ACTIVE)
        .order_by(DomainStrategyProfile.id)
        .limit(limit)
    )
    return list(session.execute(stmt).all())


@app.task(name=STRATEGY_LIGHT_RECHECK)
def light_recheck() -> None:
    """`STRATEGY_LIGHT_RECHECK` (`maintenance` queue, contracts/rediscovery.md
    "Periodic light re-check", FR-021, US4 AS4).

    Scans `ACTIVE` profiles workspace-scoped in batches (up to
    `_LIGHT_RECHECK_BATCH_SIZE` per invocation), builds `recent_signals`
    (`build_recent_signals`) + combined counts (`_combined_stats_for_profile`)
    for each, evaluates `evaluate_rediscovery`, and applies
    (`apply_rediscovery`) -- catching degradation on patrol, without
    requiring a full failed batch to have just flushed (the inline call
    site is the stats-flush task, US5 T035).

    SPEC-16 US3 (T035b, contracts/events.md #3): every profile whose
    `apply_rediscovery` call here actually returns `True` (a genuine
    ACTIVE -> DEGRADED transition) is collected and, strictly AFTER the
    single `session.commit()` below, enqueued as one `DOMAIN_STRATEGY_UPDATED`
    webhook event via `_enqueue_strategy_transition` -- this is the one
    rediscovery path `flush_profile`/`flush_stats` never sees on its own.
    """
    settings = get_settings()
    thresholds = _rediscovery_thresholds(settings)
    redis = get_redis_client()
    transitions: list[StrategyTransition] = []

    with get_session() as session:
        for profile_id, workspace_id in _scan_active_profile_refs(
            session, limit=_LIGHT_RECHECK_BATCH_SIZE
        ):
            set_workspace_context(session, workspace_id)

            profile = scoped_get(session, DomainStrategyProfile, profile_id, workspace_id)
            if profile is None or profile.status != StrategyStatus.ACTIVE:
                continue

            combined = _combined_stats_for_profile(session, redis, profile)
            recent_signals = build_recent_signals(session, profile)
            decision = evaluate_rediscovery(profile, combined, recent_signals, thresholds)

            triggered = apply_rediscovery(session, profile, decision)
            if triggered:
                logger.info(
                    "strategy_rediscovery_triggered profile_id=%s workspace_id=%s "
                    "reason=%s source=LIGHT_RECHECK",
                    profile.id,
                    workspace_id,
                    decision.reason,
                )
                transitions.append(
                    StrategyTransition(
                        profile_id=profile.id,
                        workspace_id=workspace_id,
                        domain=profile.domain,
                        new_status=StrategyStatus.DEGRADED,
                        change="REDISCOVERY_TRIGGERED",
                        method=None,
                    )
                )

        session.commit()

    for transition in transitions:
        _enqueue_strategy_transition(transition)


# --- STRATEGY_STATS_FLUSH (US5, contracts/stats-buffer.md §Flush, FR-023) --


def _scan_workspace_refs_with_profiles(session: Session) -> list[uuid.UUID]:
    """Distinct workspace ids owning at least one `domain_strategy_profiles`
    row -- the periodic `flush_stats` sweep's only anchor when invoked
    with no explicit target (the job-finalization call site already knows
    its own `workspace_id` + `profile_ids` and skips this scan entirely,
    the `_scan_job_refs`/`_scan_active_profile_refs` precedent: this
    maintenance task legitimately patrols every workspace, and every row
    is only read/mutated after `set_workspace_context` scopes it to its
    own workspace below)."""
    stmt = select(DomainStrategyProfile.workspace_id).distinct()  # noqa: workspace-scope
    return [row[0] for row in session.execute(stmt).all()]


def _enqueue_strategy_transition(transition: StrategyTransition) -> None:
    """SPEC-16 US3 (T035, contracts/events.md #3): fire-and-forget,
    post-commit webhook enqueue for one genuine strategy-status transition
    (`flush_stats`'s surfaced `flush_profile` transitions, or
    `light_recheck`'s own `triggered` rediscoveries). A broker error here
    is caught and logged, never allowed to fail/roll back the
    already-committed strategy change above (FR-009/SC-005)."""
    event_type, payload, dedup_key = build_strategy_event(
        strategy_profile_id=transition.profile_id,
        domain=transition.domain,
        new_status=transition.new_status,
        change=transition.change,
        method=transition.method,
    )
    try:
        enqueue(
            CREATE_WEBHOOK_EVENT,
            queue="webhook_events",
            kwargs={
                "workspace_id": str(transition.workspace_id),
                "event_type": event_type,
                "payload": payload,
                "dedup_key": dedup_key,
            },
        )
    except Exception:
        logger.warning(
            "webhook_enqueue_failed source=strategy profile_id=%s change=%s",
            transition.profile_id,
            transition.change,
            exc_info=True,
        )


@app.task(name=STRATEGY_STATS_FLUSH)
def flush_stats(workspace_id: str | None = None, profile_ids: list[str] | None = None) -> None:
    """`STRATEGY_STATS_FLUSH` (`maintenance` queue, contracts/stats-buffer.md
    §Flush, FR-023, SC-003).

    Two call shapes converge on the same per-profile `flush_profile`
    (`app_shared.strategy.flush`):

    - **Periodic** (no arguments -- the scheduler's cadence,
      `apps/scheduler/app/scheduler/scheduler_app.py`): scans every
      workspace that owns at least one `domain_strategy_profiles` row,
      enumerates that workspace's `stratdirty:{ws}` members
      (`SMEMBERS`), and flushes each.
    - **Job finalization** (`workspace_id` + `profile_ids` supplied,
      `apps/workers/app/workers/tasks_jobs.py::finalize_jobs`): flushes
      exactly the given profiles in that one workspace -- no Redis
      `SMEMBERS` scan needed, the caller already knows which profiles its
      just-finalized job touched.

    A `SMEMBERS`/Redis read failure for one workspace is logged and
    skipped -- it never aborts the sweep for every other workspace (a
    missed cycle just means that workspace's profiles flush one interval
    later). Emits one `strategy_stats_flushed` structured log line per
    invocation (`dirty_profiles`, `keys_flushed`) -- contracts/
    api-and-observability.md.

    SPEC-16 US3 (T035a, contracts/events.md #3): every genuine
    promotion/rediscovery transition `flush_profile` surfaces across this
    sweep is collected and, strictly AFTER the single `session.commit()`
    below, enqueued as one webhook event each via `_enqueue_strategy_transition`
    -- never pre-commit, never speculative (only transitions an `apply_*`
    call already confirmed real).
    """
    redis = get_redis_client()
    dirty_profiles = 0
    keys_flushed = 0
    transitions: list[StrategyTransition] = []

    with get_session() as session:
        if workspace_id is not None:
            ws_list = [uuid.UUID(str(workspace_id))]
        else:
            ws_list = _scan_workspace_refs_with_profiles(session)

        for ws in ws_list:
            set_workspace_context(session, ws)

            if workspace_id is not None and profile_ids is not None:
                pending_ids = [uuid.UUID(str(pid)) for pid in profile_ids]
            else:
                try:
                    pending_ids = [uuid.UUID(str(pid)) for pid in redis.smembers(dirty_key(ws))]
                except Exception:
                    logger.warning(
                        "strategy_stats_flush: failed to read stratdirty for workspace_id=%s",
                        ws,
                        exc_info=True,
                    )
                    continue

            for profile_id in pending_ids:
                dirty_profiles += 1
                result = flush_profile(session, redis, profile_id)
                keys_flushed += result.keys_flushed
                transitions.extend(result.transitions)

        session.commit()

    for transition in transitions:
        _enqueue_strategy_transition(transition)

    logger.info(
        "strategy_stats_flushed dirty_profiles=%d keys_flushed=%d",
        dirty_profiles,
        keys_flushed,
    )


# --- STRATEGY_PATTERN_BACKFILL (FR-005, D10, T041) ------------------------


#: Backfill batch size per invocation -- a local implementation constant, the
#: same precedent as `_LIGHT_RECHECK_BATCH_SIZE`.
_PATTERN_BACKFILL_BATCH_SIZE = 200


def _scan_stale_pattern_profile_refs(
    session: Session, *, limit: int
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """`(id, workspace_id)` for profiles stamped with an OLD
    `url_pattern_version` (< current `URL_PATTERN_ALGORITHM_VERSION`), unscoped
    -- the `_scan_active_profile_refs` precedent (every row is only
    read/mutated after `set_workspace_context` scopes it below). At algorithm
    version 1 (current) this returns nothing (D10 "defined mechanism, not
    exercised at version 1")."""
    stmt = (
        select(DomainStrategyProfile.id, DomainStrategyProfile.workspace_id)  # noqa: workspace-scope
        .where(DomainStrategyProfile.url_pattern_version < URL_PATTERN_ALGORITHM_VERSION)
        .order_by(DomainStrategyProfile.id)
        .limit(limit)
    )
    return list(session.execute(stmt).all())


@app.task(name=STRATEGY_PATTERN_BACKFILL)
def pattern_backfill() -> None:
    """`STRATEGY_PATTERN_BACKFILL` (`maintenance` queue, FR-005, §15
    "Pattern algorithm versioning", D10).

    When `URL_PATTERN_ALGORITHM_VERSION` is bumped, stored `url_pattern`
    values (the join key between matches and learned strategies) may no
    longer match what the new algorithm derives. This task patrols profiles
    stamped with an older version and, for each, re-derives the pattern from
    a representative `competitor_product_matches` URL of the same
    `(competitor_id, domain)`:

    * pattern unchanged -> just re-stamp `url_pattern_version` (cheap re-link);
    * pattern changed, or no representative URL exists -> re-stamp the version,
      reset `status = DISCOVERY_REQUIRED`, and enqueue `STRATEGY_DISCOVERY_RUN`
      so the strategy is re-learned under the new algorithm (never mixing
      versions in a lookup, FR-005).

    Bounded (`_PATTERN_BACKFILL_BATCH_SIZE` per invocation) and idempotent:
    once every row is at the current version the scan is empty. Enqueued
    on-demand after an algorithm bump (there is no steady-state schedule).
    """
    with get_session() as session:
        rebuilt = 0
        rediscovered = 0
        for profile_id, workspace_id in _scan_stale_pattern_profile_refs(
            session, limit=_PATTERN_BACKFILL_BATCH_SIZE
        ):
            set_workspace_context(session, workspace_id)
            profile = scoped_get(session, DomainStrategyProfile, profile_id, workspace_id)
            if profile is None or profile.url_pattern_version >= URL_PATTERN_ALGORITHM_VERSION:
                continue

            # A representative match currently grouped under this profile's
            # (competitor, pattern) -- its `competitor_url` is what the new
            # algorithm re-derives from. The competitor's single domain is
            # implied by `competitor_id`, so no domain filter is needed.
            sample = session.execute(
                scoped_select(CompetitorProductMatch, workspace_id)
                .where(
                    CompetitorProductMatch.competitor_id == profile.competitor_id,
                    CompetitorProductMatch.url_pattern == profile.url_pattern,
                )
                .limit(1)
            ).scalars().first()

            requeue = True
            if sample is not None:
                new_pattern = derive_url_pattern(sample.competitor_url)
                if new_pattern == profile.url_pattern:
                    requeue = False
                else:
                    profile.url_pattern = new_pattern

            profile.url_pattern_version = URL_PATTERN_ALGORITHM_VERSION
            if requeue:
                profile.status = StrategyStatus.DISCOVERY_REQUIRED
                enqueue(
                    STRATEGY_DISCOVERY_RUN,
                    queue=_DISCOVERY_QUEUE,
                    kwargs={
                        "workspace_id": str(workspace_id),
                        "competitor_id": str(profile.competitor_id),
                        "domain": profile.domain,
                        "url_pattern": profile.url_pattern,
                        "sample_urls": [],
                        "triggered_by": "AUTO",
                    },
                )
                rediscovered += 1
            else:
                rebuilt += 1

        session.commit()

    logger.info(
        "strategy_pattern_backfill relinked=%d rediscovery_enqueued=%d target_version=%d",
        rebuilt,
        rediscovered,
        URL_PATTERN_ALGORITHM_VERSION,
    )
