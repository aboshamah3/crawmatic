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
plus `scrape_core.safety.fetch.validate_resolved_target`
for the SSRF guard (save-time checks AND the fetch-time
resolve-then-check every other fetch path in this system performs --
see `_probe_get`), and the existing `app_shared.access`
provider/assignment plumbing for `PROXY_HTTP`. Fully off-reactor (a
Celery task, never the Twisted reactor/Scrapy) -- blocking HTTP calls
here are safe and expected (Constitution V).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.workers.celery_app import app
from app_shared.access.breaker import log_denied, paid_requests_allowed
from app_shared.access.repository import visible_providers_select
from app_shared.config import Settings, get_settings
from app_shared.database import get_session, get_system_session, set_workspace_context
from app_shared.ids import new_uuid7
from app_shared.enums import (
    AccessMethod,
    DiscoveryRunStatus,
    ExtractionMethod,
    MethodType,
    ProxyProviderStatus,
    RequestOrigin,
    StrategyStatus,
)
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.observations import RequestAttempt
from app_shared.models.strategy import DomainStrategyProfile, StrategyDiscoveryRun
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.redis_client import get_redis_client
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.encryption import SecretDecryptionError, decrypt_secret
from app_shared.strategy.flush import (
    StrategyTransition,
    flush_profile,
    rebase_stats_after_discovery,
)
from app_shared.strategy.promotion import PromotionThresholds
from app_shared.strategy.rediscovery import (
    CombinedStats,
    RediscoveryThresholds,
    apply_rediscovery,
    build_recent_signals,
    evaluate_rediscovery,
)
from app_shared.outbox import write_outbox_message
from app_shared.strategy.repository import resolve_profile, stats_for_profile
from app_shared.strategy.seed import DiscoverySeedConfidences, seed_from_discovery, validate_sample_size
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
from scrape_core.safety.fetch import Resolver, system_resolver, validate_resolved_target
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

#: Realistic browser headers for probe fetches. `requests`' default
#: `python-requests/x.y` User-Agent is a canonical bot-block target --
#: live jarir.com 403s it in ~0.1s (verified 2026-07-11), which made
#: every jarir discovery probe fail instantly and the run end NO_WINNER
#: despite the pages being fully fetchable. The same page serves 200 to
#: a browser UA, so a probe that claims to measure "is DIRECT_HTTP
#: viable for this domain" must present one.
_PROBE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en",
}

#: Maximum redirect hops one probe fetch will follow. `requests` would
#: follow them itself (and silently), but then only the FIRST URL would
#: ever be SSRF-checked -- so redirects are followed by hand here, one
#: validated hop at a time, exactly as Scrapy's `RedirectMiddleware`
#: re-emits each `Location` through `SsrfGuardMiddleware`/`SafeResolver`
#: for the spider path.
_MAX_PROBE_REDIRECTS = 5

#: Injectable resolver seam for the fetch-time SSRF guard, mirroring
#: `scrape_core.browser.ssrf.abort_unsafe_request`'s `resolver=` argument:
#: production uses the real system resolver, tests substitute a fake that
#: returns a canned private/public address. Module-level (not `_`-nested)
#: purely so tests can monkeypatch it, the same convention
#: `_match_ids_for_urls` already uses here.
_probe_resolver: Resolver = system_resolver


def _probe_get(url: str, **kwargs: Any) -> "requests.Response | None":
    """One probe fetch with the SAME fetch-time SSRF guard the spiders use.

    ## Why this exists (audit H7)

    `strategy_discovery_run` validates its sample URLs once, with
    `app_shared.url_safety.validate_competitor_url` -- the **save-time**
    validator, which by its own docstring performs no DNS resolution.
    Every other fetch path in this system additionally performs the
    resolve-then-check step at fetch time: the HTTP spider through
    `safety.resolver.SafeResolver` (`DNS_RESOLVER`), the browser spider
    through `browser.ssrf.abort_unsafe_request`
    (`PLAYWRIGHT_ABORT_REQUEST`). These off-reactor probe fetches did
    not, which left two real gaps:

    * **DNS rebinding** -- a hostname that resolved public when the
      match was saved (or milliseconds ago, at `validate_competitor_url`
      time) can resolve to `169.254.169.254`/`127.0.0.1`/an RFC1918
      address by the time `requests` connects. The save-time check
      cannot see that; only a resolve-then-check at fetch time can.
    * **Redirects** -- `requests.get` follows `Location` itself, so a
      public first hop could redirect straight into the internal
      network with nothing re-validating the new target.

    Both are closed by reusing, never re-implementing,
    `scrape_core.safety.fetch.validate_resolved_target` (the same
    function behind both spider guards): validate the target, fetch ONE
    hop with `allow_redirects=False`, then validate and follow each
    `Location` in turn, bounded by `_MAX_PROBE_REDIRECTS`.

    Returns the final `requests.Response`, or `None` when a hop is
    refused / unresolvable / the redirect budget is exhausted. Fails
    closed: a host that cannot be resolved is never treated as safe.
    `requests.RequestException` propagates to the caller, which already
    records it as "no qualifying observation" for this combo/url.
    """
    current = url
    for _ in range(_MAX_PROBE_REDIRECTS + 1):
        try:
            validate_resolved_target(current, resolver=_probe_resolver)
        except UnsafeUrlError as exc:
            logger.warning(
                "strategy_discovery: probe target refused by SSRF guard url=%s reason=%s",
                current,
                exc.reason,
            )
            return None
        except OSError as exc:
            # Fail closed -- an unresolvable/erroring host is never safe.
            logger.info(
                "strategy_discovery: probe target could not be resolved url=%s error=%s",
                current,
                exc,
            )
            return None

        response = requests.get(
            current, timeout=_PROBE_TIMEOUT_SECONDS, allow_redirects=False, **kwargs
        )
        location = response.headers.get("location") if response.is_redirect else None
        if not location:
            return response
        current = urljoin(current, location)

    logger.info("strategy_discovery: probe exceeded the redirect budget url=%s", url)
    return None


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
    scope: str = "domain",
) -> list[str]:
    """Auto-trigger fallback (contracts/discovery.md "Payload"): select up
    to `max_sample` matched URLs for this key from `competitor_product_matches`
    when the caller didn't supply `sample_urls` (US2's AUTO enqueue, empty list).

    `scope == "domain"` (default, `Settings.STRATEGY_PROFILE_SCOPE`): the
    `url_pattern` filter is dropped -- `competitor_id` + the workspace RLS
    scoping already bound the sample to the domain, so this gate no longer
    depends on stored match `url_pattern` values at all (fixes the
    discovery gate never firing for per-product-slug catalogs, where every
    match is its own n=1 pattern). `scope == "url_pattern"` keeps the exact
    legacy filtered behavior."""
    stmt = scoped_select(CompetitorProductMatch, workspace_id).where(
        CompetitorProductMatch.competitor_id == competitor_id,
    )
    if scope != "domain":
        stmt = stmt.where(CompetitorProductMatch.url_pattern == url_pattern)
    stmt = stmt.limit(max_sample)
    return [row.competitor_url for row in session.execute(stmt).scalars().all()]


# --- probe accounting (Task 2.3, proxy-cost-reduction plan §2.3) ----------
#
# Discovery probes made ~87,000 real proxy requests but wrote 646
# `request_attempts` rows (see `_fetch_via_proxy`'s docstring) -- per-URL
# accounting and the `REQUESTS_PER_URL` circuit-breaker condition were
# blind to the largest paid source. `_probe_sample` now writes one
# `RequestAttempt` row per probe fetch it attempts, tagged
# `origin=RequestOrigin.DISCOVERY`, so both the breaker's per-URL ratio
# AND the money-spent counters finally see this traffic. Readers that
# score *scrape* outcomes (the daily rollup, `build_recent_signals`) must
# filter to `origin='scrape'` so this deliberately noisy, deliberately
# multi-method ladder can never be misread as a real scrape degrading
# (contracts/rediscovery.md condition 6, the Task 3.3 prerequisite) --
# spend/volume accounting deliberately does NOT filter, since a discovery
# probe's proxy request costs exactly the same money as a scrape one.


def _match_ids_for_urls(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    urls: list[str],
) -> dict[str, uuid.UUID]:
    """Best-effort `competitor_url -> CompetitorProductMatch.id` lookup for
    the given `urls` (`RequestAttempt.match_id` is NOT NULL, so a probe
    attempt can only be recorded when its URL already has a match). A
    read-only, workspace-scoped query -- exposed at module scope (not
    `_`-nested) purely so tests can monkeypatch it, the same convention
    `test_discovery_early_exit.py` already uses for `_fetch`."""
    if not urls:
        return {}
    stmt = scoped_select(CompetitorProductMatch, workspace_id).where(
        CompetitorProductMatch.competitor_id == competitor_id,
        CompetitorProductMatch.competitor_url.in_(urls),
    )
    rows = session.execute(stmt).scalars().all()
    return {row.competitor_url: row.id for row in rows}


def _record_probe_attempt(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    match_id: uuid.UUID | None,
    access_method: AccessMethod,
    url: str,
    success: bool,
) -> None:
    """Best-effort write of one discovery-probe `RequestAttempt` row
    (`origin=DISCOVERY`). A no-op when `match_id` is `None` -- an
    operator-supplied ad hoc sample URL with no `CompetitorProductMatch`
    yet has nothing to attribute the row to
    (`tests/integration/test_discovery_run.py`'s scenarios are exactly
    this shape). Never raises: this is accounting, not the probe itself --
    a lost row must never fail a discovery run, mirroring `stats_buffer
    .record_attempt`'s fail-open telemetry posture
    (contracts/stats-buffer.md step 4)."""
    if match_id is None:
        return
    try:
        session.add(
            RequestAttempt(
                workspace_id=workspace_id,
                created_at=datetime.now(timezone.utc),
                match_id=match_id,
                attempt_number=1,
                url=url,
                access_method=access_method,
                success=success,
                origin=RequestOrigin.DISCOVERY,
            )
        )
    except Exception:  # noqa: BLE001 - accounting must never break a probe
        logger.warning(
            "strategy_discovery: failed to record probe attempt url=%s access_method=%s",
            url,
            access_method.value,
            exc_info=True,
        )


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
            response = _probe_get(url, headers=_PROBE_HEADERS)
        except requests.RequestException as exc:
            logger.info(
                "strategy_discovery: direct fetch failed url=%s attempt=%d error=%s",
                url,
                attempt,
                exc,
            )
            continue
        if response is None:
            # Refused by the fetch-time SSRF guard (or unresolvable) --
            # a retry would resolve the same host, so stop here.
            return None
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

    Credentials go in the **proxy URL userinfo**, not a
    `Proxy-Authorization` header (2026-08-03 fix). `requests` reaches an
    https:// target through a CONNECT tunnel, and anything in `headers=`
    is sent to the destination *inside* that tunnel — the proxy never
    sees it, so every probe died `407 NO_USER`. Because a failed probe is
    indistinguishable from "this method doesn't work for this domain",
    discovery could never confirm a proxied method and kept promoting
    DIRECT ones: that is why stech.ink held `DIRECT_HTTP` while
    accumulating 996 recorded failures. The spider path was never
    affected — Scrapy authenticates CONNECT from its own
    `Proxy-Authorization` meta.

    Otherwise mirrors `generic_price_spider`'s provider selection
    (`decrypt_secret`), simplified to a one-off probe: no rotation/
    stickiness policy applies to a single discovery sample.
    """
    providers = session.execute(visible_providers_select(workspace_id)).scalars().all()
    active = [p for p in providers if p.status == ProxyProviderStatus.ACTIVE]
    if not active:
        return None

    provider = active[0]
    proxy_url = provider.base_url if "://" in provider.base_url else f"http://{provider.base_url}"

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
        proxy_url = _proxy_url_with_credentials(proxy_url, provider.username, password)

    return {"proxies": {"http": proxy_url, "https": proxy_url}, "headers": {}}


def _proxy_url_with_credentials(proxy_url: str, username: str, password: str) -> str:
    """Embed `username`/`password` in `proxy_url`'s userinfo.

    This is the only way `requests` authenticates a CONNECT tunnel — see
    `_build_proxy_kwargs`. Both parts are percent-encoded (a DataImpulse
    username carries `__cr.sa`/`;sessid.` and a password may contain any
    byte), with `safe=""` so `:`/`@`/`/` can never split the URL.
    """
    scheme, _, rest = proxy_url.partition("://")
    if not rest:
        scheme, rest = "http", proxy_url
    rest = rest.rpartition("@")[2]  # never double-embed credentials
    return f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{rest}"


def _fetch_via_proxy(session: Session, workspace_id: uuid.UUID, url: str) -> str | None:
    """One `PROXY_HTTP` fetch attempt; `None` when no provider is
    configured, the circuit breaker is OPEN, or the request fails --
    never raises.

    ## Breaker gate (2026-08-15)

    This is the only leg of the discovery probe ladder that spends money,
    and it was the single largest source of paid requests in the system
    (measured: 87,082 DataImpulse requests for www.extra.com against 646
    recorded `request_attempts` rows -- discovery probes are not written
    to the audit table at all, so every Redis/Postgres accounting path
    was blind to them). It also never consulted
    `access.breaker.paid_requests_allowed`, so an OPEN circuit breaker
    stopped the spider's paid work while discovery kept probing through
    the same proxy. An OPEN breaker now skips this leg exactly as it
    degrades a proxied spider plan: `PROXY_HTTP` simply stops being a
    discovery candidate, which is the same outcome as "no provider
    configured" and is already handled everywhere downstream.
    """
    allowed, reason = paid_requests_allowed(get_session)
    if not allowed:
        log_denied(domain=urlsplit(url).hostname or "", reason=reason)
        logger.warning(
            "strategy_discovery: proxy probe denied by circuit breaker url=%s reason=%s",
            url,
            reason,
        )
        return None

    proxy_kwargs = _build_proxy_kwargs(session, workspace_id)
    if proxy_kwargs is None:
        return None
    proxy_kwargs["headers"] = {**_PROBE_HEADERS, **proxy_kwargs.get("headers", {})}
    try:
        response = _probe_get(url, **proxy_kwargs)
    except requests.RequestException as exc:
        logger.info("strategy_discovery: proxy fetch failed url=%s error=%s", url, exc)
        return None
    if response is None:
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
    session: Session,
    *,
    workspace_id: uuid.UUID,
    urls: list[str],
    thresholds: PromotionThresholds,
    competitor_id: uuid.UUID | None = None,
) -> dict[tuple[AccessMethod, ExtractionMethod], _Tally]:
    """Drive `urls` through each candidate access method, then the reused
    extraction chain, tallying qualifying observations per `(access,
    extraction)` combo (contracts/discovery.md steps 3-4).

    `competitor_id` (Task 2.3, optional/keyword so older callers/tests
    that predate probe accounting keep working unmodified) resolves each
    URL's `CompetitorProductMatch.id` ONCE up front
    (`_match_ids_for_urls`) so every probed `(access_method, url)`
    attempt -- every leg this loop actually calls `_fetch` for, including
    the paid `PROXY_HTTP` one via `_fetch_via_proxy` -- gets one
    `origin=DISCOVERY` `RequestAttempt` row (`_record_probe_attempt`).
    `_fetch`/`_fetch_direct`/`_fetch_via_proxy` are never reached from
    anywhere else in this module, so this single write site covers all of
    them without double-counting."""
    confidence_cfg = resolve_confidence_rules(
        {"min_accepted_confidence": float(thresholds.confidence_threshold)}
    )
    tallies: dict[tuple[AccessMethod, ExtractionMethod], _Tally] = {}

    match_ids: dict[str, uuid.UUID] = {}
    if competitor_id is not None:
        try:
            match_ids = _match_ids_for_urls(
                session, workspace_id=workspace_id, competitor_id=competitor_id, urls=urls
            )
        except Exception:  # noqa: BLE001 - accounting must never break a probe
            logger.warning(
                "strategy_discovery: failed to resolve match ids for probe accounting "
                "competitor_id=%s",
                competitor_id,
                exc_info=True,
            )
            match_ids = {}

    for access_method in _ACCESS_LADDER:
        for url in urls:
            html = _fetch(session, workspace_id, access_method, url)
            _record_probe_attempt(
                session,
                workspace_id=workspace_id,
                match_id=match_ids.get(url),
                access_method=access_method,
                url=url,
                success=html is not None,
            )
            if html is None:
                continue

            try:
                candidate = extract(html)
            except Exception:  # noqa: BLE001
                # One pathological page (e.g. a payload that crashes a
                # parser) must cost only its own observation, never the
                # whole run -- pre-guard, a single such URL FAILED the
                # entire discovery task for its domain (seen live on
                # amazon.sa, 2026-07-11).
                logger.warning(
                    "strategy_discovery: extraction crashed url=%s access_method=%s",
                    url,
                    access_method.value,
                    exc_info=True,
                )
                continue
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

        # 2026-08-11 proxy-cost Fix 4b (PLAN_PROXY_COST_REDUCTION.md):
        # once an access method qualifies on the ENTIRE sample, no later
        # ladder leg can beat it -- `select_discovery_winner` ranks by
        # most qualifying URLs first and breaks ties by CHEAPEST access,
        # and the ladder is walked cheapest-first. Probing the remaining
        # (more expensive, possibly proxied) legs could only ever tie and
        # then lose the tie-break, so the outcome is provably identical
        # and the paid fetches are pure waste (up to 2 legs x 10 URLs per
        # new discovery key).
        sample = set(urls)
        if any(
            method is access_method and tally.qualifying_urls >= sample
            for (method, _extraction), tally in tallies.items()
        ):
            logger.info(
                "strategy_discovery: access_method=%s qualified on the full "
                "sample (%d urls) -- skipping the remaining ladder legs",
                access_method.value,
                len(sample),
            )
            break

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


#: How long a `RUNNING` discovery run suppresses a duplicate AUTO
#: delivery for the same `(competitor, url_pattern)` key. Comfortably
#: above the probe loop's ~600s worst case (3 ladder legs x 10 sample
#: URLs x 15s) yet bounded, so a run wedged `RUNNING` by a hard kill
#: cannot block that key's discovery forever.
_AUTO_RUN_DEDUP_SECONDS = 3600


def _auto_run_in_flight(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    url_pattern: str,
    now: datetime,
) -> bool:
    """`True` if a recent `RUNNING` run already covers this discovery key.

    The AUTO trigger carries no `run_id`, so this is its replay guard
    (audit H1) — see `run_discovery`. Scoped to the workspace like every
    other read in this module.
    """
    cutoff = now - timedelta(seconds=_AUTO_RUN_DEDUP_SECONDS)
    existing = (
        session.execute(
            scoped_select(StrategyDiscoveryRun, workspace_id)
            .where(
                StrategyDiscoveryRun.competitor_id == competitor_id,
                StrategyDiscoveryRun.url_pattern == url_pattern,
                StrategyDiscoveryRun.status == DiscoveryRunStatus.RUNNING,
                StrategyDiscoveryRun.created_at >= cutoff,
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    return existing is not None


def _runs_in_trailing_day(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    url_pattern: str,
    now: datetime,
) -> int:
    """How many discovery runs this exact key already recorded in the
    trailing 24h — the ledger the per-key ceiling reads.

    `strategy_discovery_runs` is the durable, append-only record of what
    actually happened (the same table `access/breaker.py`'s
    `DISCOVERY_RUNS_PER_DOMAIN` condition reads), indexed by
    `ix_sdr_ws_competitor_domain_pattern`, so no new table or counter is
    needed for the bound.
    """
    cutoff = now - timedelta(hours=24)
    stmt = (
        scoped_select(StrategyDiscoveryRun, workspace_id)
        .where(
            StrategyDiscoveryRun.competitor_id == competitor_id,
            StrategyDiscoveryRun.url_pattern == url_pattern,
            StrategyDiscoveryRun.created_at >= cutoff,
        )
        .with_only_columns(func.count())
        .order_by(None)
    )
    return int(session.execute(stmt).scalar_one() or 0)


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
            # Idempotency guard (2026-08-15 audit risk H1). Under
            # `task_acks_late` a worker killed mid-probe has its message
            # redelivered, and the outbox publishes at-least-once — so
            # this task MUST be replay-safe. It is the most expensive
            # task in the system to replay: the probe loop below walks
            # the access ladder over the whole sample and its
            # `PROXY_HTTP` leg spends real money per request. A run that
            # is no longer PENDING has already been claimed (RUNNING) or
            # finished, so the replay is a no-op instead of a second
            # paid sample.
            if run is not None and run.status is not DiscoveryRunStatus.PENDING:
                logger.info(
                    "strategy_discovery: skipping replay of non-PENDING run_id=%s status=%s",
                    run.id,
                    run.status.value,
                )
                return
        elif _auto_run_in_flight(
            session,
            workspace_id=ws,
            competitor_id=comp_id,
            url_pattern=url_pattern,
            now=datetime.now(timezone.utc),
        ):
            # Same guard for the AUTO path, which has no caller-supplied
            # run id to key on: another delivery of this same logical
            # discovery is already probing (or crashed mid-probe less
            # than `_AUTO_RUN_DEDUP_SECONDS` ago). Bounded by that window
            # so a run wedged in RUNNING by a hard kill can never block
            # genuine later discovery for this key forever.
            logger.info(
                "strategy_discovery: skipping replay, run already in flight "
                "competitor_id=%s url_pattern=%s",
                comp_id,
                url_pattern,
            )
            return

        # --- Per-key daily ceiling (2026-08-15 runaway backstop) --------
        # The structural bound that a correctness bug cannot talk its way
        # past. `apply_rediscovery`'s cooldown bounds the one enqueue path
        # it owns; this bounds EVERY path into this task, including any
        # future one, because it is enforced here at the point of spend.
        #
        # Deliberately scoped to machine-triggered runs (`run_id is
        # None`, i.e. AUTO and REDISCOVERY). An operator run
        # (`POST /v1/strategy/discovery-runs`) has already created its
        # PENDING row and is waiting on it; silently refusing to execute
        # it would strand the run and hide the refusal from the human who
        # asked. Machine triggers are the ones that can loop.
        #
        # No row is written when the ceiling refuses, so the ledger this
        # reads never inflates itself and the bound releases on its own
        # 24h after the last real run — unlike `proxy_circuit_breakers`,
        # which is a fleet-wide kill switch with deliberately manual
        # recovery. The two are complementary: this keeps one key from
        # burning the fleet's allowance; the breaker is what stops the
        # fleet if something still does.
        max_runs_per_day = int(settings.STRATEGY_DISCOVERY_MAX_RUNS_PER_KEY_PER_DAY)
        if run_id is None and max_runs_per_day > 0:
            recent_runs = _runs_in_trailing_day(
                session,
                workspace_id=ws,
                competitor_id=comp_id,
                url_pattern=url_pattern,
                now=datetime.now(timezone.utc),
            )
            if recent_runs >= max_runs_per_day:
                logger.warning(
                    "strategy_discovery: strategy_discovery_rate_limited "
                    "workspace_id=%s competitor_id=%s domain=%s url_pattern=%s "
                    "runs_24h=%d max_runs_per_day=%d triggered_by=%s",
                    ws,
                    comp_id,
                    domain,
                    url_pattern,
                    recent_runs,
                    max_runs_per_day,
                    triggered_by,
                )
                return

        urls = list(sample_urls or [])
        if not urls:
            urls = _select_sample_urls(
                session,
                workspace_id=ws,
                competitor_id=comp_id,
                url_pattern=url_pattern,
                max_sample=settings.STRATEGY_DISCOVERY_MAX_SAMPLE,
                scope=settings.STRATEGY_PROFILE_SCOPE,
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
                session, workspace_id=ws, urls=safe_urls, thresholds=thresholds, competitor_id=comp_id
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
        session.flush()
        # 2026-08-15 runaway-rediscovery root fix, second half. Seeding
        # alone left rediscovery condition 2 reading the *lifetime*
        # success ratio that had just triggered this very run, so the
        # next 60s light-recheck tick re-triggered on identical inputs.
        # Re-base the winning methods' counters onto this run's own
        # result -- see `rebase_stats_after_discovery`'s docstring for
        # the measured loop (fqtoners.com, 13,151 runs / 1,439 per day).
        rebase_stats_after_discovery(
            session,
            profile.id,
            winning_access=access_method,
            winning_extraction=extraction_method,
            confidence=confidences.access_confidence,
            qualifying_count=confidences.access_qualifying_count,
            now=now,
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


def _scan_active_profile_refs(*, limit: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Resolve `(id, workspace_id)` pairs for `ACTIVE` profiles, unscoped.

    A periodic maintenance sweep necessarily spans every workspace, and
    under FORCE ROW LEVEL SECURITY the ordinary engine's role fail-closes
    an unscoped scan to ZERO rows when no workspace context is set --
    which silently killed finalization for 6.5 h on 2026-08-21 (mushtryati
    F-1). So the id-pair scan runs on the sanctioned BYPASSRLS system
    session (`get_system_session`, the outbox-dispatcher / scheduler-claim
    precedent, and now `_scan_job_refs` in `tasks_jobs.py`) and returns
    plain ids; EVERY subsequent row read/write happens on the caller's
    ordinary session, re-scoped per profile via `set_workspace_context` --
    the system session never touches a row.
    """
    with get_system_session() as session:
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
            limit=_LIGHT_RECHECK_BATCH_SIZE
        ):
            set_workspace_context(session, workspace_id)

            profile = scoped_get(session, DomainStrategyProfile, profile_id, workspace_id)
            if profile is None or profile.status != StrategyStatus.ACTIVE:
                continue

            combined = _combined_stats_for_profile(session, redis, profile)
            recent_signals = build_recent_signals(session, profile)
            decision = evaluate_rediscovery(
                profile, combined, recent_signals, thresholds, scope=settings.STRATEGY_PROFILE_SCOPE
            )

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

        for transition in transitions:
            _outbox_strategy_transition(session, transition)

        session.commit()


# --- STRATEGY_STATS_FLUSH (US5, contracts/stats-buffer.md §Flush, FR-023) --


def _scan_workspace_refs_with_profiles() -> list[uuid.UUID]:
    """Distinct workspace ids owning at least one `domain_strategy_profiles`
    row -- the periodic `flush_stats` sweep's only anchor when invoked
    with no explicit target (the job-finalization call site already knows
    its own `workspace_id` + `profile_ids` and skips this scan entirely).

    A periodic maintenance sweep necessarily spans every workspace, and
    under FORCE ROW LEVEL SECURITY the ordinary engine's role fail-closes
    an unscoped scan to ZERO rows when no workspace context is set --
    which silently killed finalization for 6.5 h on 2026-08-21 (mushtryati
    F-1). So this scan runs on the sanctioned BYPASSRLS system session
    (`get_system_session`, the `_scan_job_refs`/`_scan_active_profile_refs`
    precedent) and returns plain ids; every subsequent row read/write
    happens on the caller's ordinary session, re-scoped per workspace via
    `set_workspace_context` -- the system session never touches a row.
    """
    with get_system_session() as session:
        stmt = select(DomainStrategyProfile.workspace_id).distinct()  # noqa: workspace-scope
        return [row[0] for row in session.execute(stmt).all()]


def _outbox_strategy_transition(session: Session, transition: StrategyTransition) -> None:
    """SPEC-16 US3 (T035, contracts/events.md #3), reworked for audit H1.

    Records the webhook event for one genuine strategy-status transition
    (`flush_stats`'s surfaced `flush_profile` transitions, or
    `light_recheck`'s own `triggered` rediscoveries) as an
    `outbox_messages` row in the **caller's still-open transaction** —
    the same transaction that carries the status change itself.

    It replaces a post-commit `enqueue` whose broker error was caught and
    logged. That seam was silently lossy in exactly the situation it
    mattered: a Redis interruption during a degradation storm dropped the
    very events that would have told an operator the strategies were
    degrading. Now the event either commits with the transition or
    disappears with it, and the outbox dispatcher publishes it later.
    """
    event_type, payload, dedup_key = build_strategy_event(
        strategy_profile_id=transition.profile_id,
        domain=transition.domain,
        new_status=transition.new_status,
        change=transition.change,
        method=transition.method,
    )
    # The message id doubles as the consumer's idempotency key -- see
    # `create_webhook_event`.
    message_id = new_uuid7()
    now = datetime.now(timezone.utc)
    write_outbox_message(
        session,
        workspace_id=transition.workspace_id,
        task_name=CREATE_WEBHOOK_EVENT,
        queue="webhook_events",
        kwargs={
            "workspace_id": str(transition.workspace_id),
            "event_type": event_type,
            "payload": payload,
            "dedup_key": dedup_key,
            "event_id": str(message_id),
            "occurred_at": now.isoformat(),
        },
        dedup_key=dedup_key,
        now=now,
        message_id=message_id,
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
            ws_list = _scan_workspace_refs_with_profiles()

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

        for transition in transitions:
            _outbox_strategy_transition(session, transition)

        session.commit()

    logger.info(
        "strategy_stats_flushed dirty_profiles=%d keys_flushed=%d",
        dirty_profiles,
        keys_flushed,
    )


# --- STRATEGY_PATTERN_BACKFILL (FR-005, D10, T041) ------------------------


#: Backfill batch size per invocation -- a local implementation constant, the
#: same precedent as `_LIGHT_RECHECK_BATCH_SIZE`.
_PATTERN_BACKFILL_BATCH_SIZE = 200


def _scan_stale_pattern_profile_refs(*, limit: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """`(id, workspace_id)` for profiles stamped with an OLD
    `url_pattern_version` (< current `URL_PATTERN_ALGORITHM_VERSION`), unscoped.
    At algorithm version 1 (current) this returns nothing (D10 "defined
    mechanism, not exercised at version 1").

    A periodic maintenance sweep necessarily spans every workspace, and
    under FORCE ROW LEVEL SECURITY the ordinary engine's role fail-closes
    an unscoped scan to ZERO rows when no workspace context is set --
    which silently killed finalization for 6.5 h on 2026-08-21 (mushtryati
    F-1). So this scan runs on the sanctioned BYPASSRLS system session
    (`get_system_session`, the `_scan_job_refs`/`_scan_active_profile_refs`
    precedent) and returns plain ids; every subsequent row read/write
    happens on the caller's ordinary session, re-scoped per profile via
    `set_workspace_context` -- the system session never touches a row.
    """
    with get_system_session() as session:
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
            limit=_PATTERN_BACKFILL_BATCH_SIZE
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
                # Audit H1: this was a *pre-commit* `enqueue` of PAID
                # work — if the backfill transaction rolled back, the
                # profile kept its old pattern/status while a discovery
                # run (whose PROXY_HTTP leg costs money per request)
                # still fired against the abandoned decision. Recorded in
                # the outbox instead, it commits with the decision or not
                # at all. `dedup_key` collapses repeat backfill passes
                # over the same profile into one pending run.
                write_outbox_message(
                    session,
                    workspace_id=workspace_id,
                    task_name=STRATEGY_DISCOVERY_RUN,
                    queue=_DISCOVERY_QUEUE,
                    kwargs={
                        "workspace_id": str(workspace_id),
                        "competitor_id": str(profile.competitor_id),
                        "domain": profile.domain,
                        "url_pattern": profile.url_pattern,
                        "sample_urls": [],
                        "triggered_by": "AUTO",
                    },
                    dedup_key=f"discovery:backfill:{profile.id}",
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
