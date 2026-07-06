"""``generic_browser_price_spider`` -- the SPEC-14 US1 MVP browser spider
(`contracts/browser-spider.md`).

Loads each browser-mode match's already-resolved scrape profile + access
policy via the shared :mod:`scrape_core.targets` machinery (identical to
the HTTP spider, Constitution Principle I -- neither `apps/*` project
imports the other), decides + gates the single attempt via the same
``_prepare_dispatch``/``dispatch_admission`` seam, then renders the page
in a real Chromium browser via ``scrapy-playwright``: waits for the
resolved profile's ``wait_for_selector`` (or an explicit
``networkidle`` settle when none is configured, analyze B1) bounded by
the effective timeout (`scrape_core.browser.page`), extracts + validates
the **rendered** DOM exactly as the HTTP spider does, and persists via
the same :class:`~scrape_core.pipelines.BatchedPersistencePipeline`.

R4 (`research.md`): the browser path is single-attempt, no-retry -- there
is no ``_dispatch`` ladder here (unlike the HTTP spider's multi-attempt
``errback``); one failure is terminal (job-level re-scrape handles
retry).

R7: every browser fetch is recorded under ``AccessMethod.PLAYWRIGHT_PROXY``
-- the only real transport this spider ever dispatches -- regardless of
which HTTP-shaped `AccessMethod` the shared `_prepare_dispatch` decision
returned for attempt 1 (`app_shared.access.engine.next_attempt` was
designed for the HTTP escalation ladder); `decision.plan.use_proxy` still
carries whether an actual upstream proxy should be used for this fetch.
When proxied (US4, T032), `_browser_request_for` routes the fetch through
a per-provider Playwright browser context (`playwright_context`/
`playwright_context_kwargs`, `contracts/browser-safety.md` "Proxy") built
from the already-decrypted password `load_targets` stashed on
`self._provider_passwords` (never decrypted here, never logged) --
`proxy_provider_id`/`proxy_country` are stamped for the reused SPEC-10
audit. An unproxied target still uses the default Playwright context, no
proxy kwargs, and still `PLAYWRIGHT_PROXY` with null proxy fields (R5).

No alert/variant-state/webhook computed here (FR-006/FR-020) -- the
spider stops at persistence, exactly like the HTTP spider.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import scrapy
from scrapy.http import Response

from app_shared.access.engine import AttemptPlan, ProxyAssignment
from app_shared.config import get_settings
from app_shared.enums import AccessMethod, ScrapeErrorCode
from app_shared.models.access import ProxyProvider
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.redis_client import get_redis_client

from scrape_core.browser.page import build_page_methods, effective_timeout
from scrape_core.browser.variant import VariantConfigError
from scrape_core.db import run_in_thread
from scrape_core.errors import (
    PRICE_NOT_FOUND,
    classify_exception,
    classify_http_status,
    classify_playwright_exception,
)
from scrape_core.extraction.pipeline import extract
from scrape_core.items import ScrapeResult
from scrape_core.limiter import LockGrant, Permission, release_slot
from scrape_core.result_builder import build_scrape_result
from scrape_core.targets import (
    AdmissionContext,
    VisibleProviders,
    _attempt_kwargs_from_meta,
    _parse_host_port,
    _parse_match_ids,
    _prepare_dispatch,
    _RequeueState,
    SpiderTarget,
    dispatch_admission,
    load_targets,
)
from scrape_core.validation import Accepted, Rejected, validate_candidate

logger = logging.getLogger(__name__)

_DEFAULT_MODE = "BROWSER"

__all__ = ["GenericBrowserPriceSpider", "classify_browser_failure"]


def _variant_selectors(target: "SpiderTarget") -> set[str]:
    """Every CSS selector `target.variant_selector_config`'s ``actions``/
    ``settle`` step address (US3, T027) -- a best-effort signal (see
    :func:`classify_browser_failure`) to recognize that a run-time
    Playwright failure happened while interacting with a *variant*
    element specifically, as opposed to the profile's own
    ``wait_for_selector`` or a plain navigation timeout. Tolerates a
    malformed ``variant_selector_config`` (returns whatever selectors it
    can find) -- this is purely a classification aid, never a validator
    (that's `scrape_core.browser.variant.parse_variant_config`'s job).
    """
    config = target.variant_selector_config
    if not isinstance(config, dict):
        return set()
    selectors: set[str] = set()
    actions = config.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                selector = action.get("selector")
                if isinstance(selector, str) and selector:
                    selectors.add(selector)
    settle = config.get("settle")
    if isinstance(settle, dict):
        settle_selector = settle.get("wait_for_selector")
        if isinstance(settle_selector, str) and settle_selector:
            selectors.add(settle_selector)
    return selectors


_PROXY_FAILURE_MARKERS = ("proxy", "err_tunnel", "err_no_supported_proxies")


def _looks_like_proxy_failure(exc: BaseException) -> bool:
    """Duck-typed-by-message proxy-connect/context-creation signal (T033).

    Playwright wraps every failure (a malformed `proxy` context kwarg, a
    proxy CONNECT/tunnel refusal, an auth rejection, ...) in its own
    generically-named ``Error``/``TimeoutError`` classes -- there is no
    dedicated Playwright exception type to recognize by class, unlike
    Scrapy's own ``TunnelError`` the HTTP path's ``classify_exception``
    checks by name (SPEC-10 US3). Chromium's underlying network-stack
    messages for a proxy failure (context-creation-time or at the first
    navigation attempt through it -- Chromium does not distinguish the
    two for our purposes) consistently mention "proxy" or one of the
    ``net::ERR_*`` proxy codes, so this mirrors that same convention
    against the exception's message instead of its class name. Ordinary
    navigation timeouts/selector waits never match (Playwright's own
    ``TimeoutError`` message is just ``"Timeout <n>ms exceeded."``), so
    this never shadows the generic ``TIMEOUT``/``PLAYWRIGHT_FAILED``
    classification for a target that wasn't proxied.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _PROXY_FAILURE_MARKERS)


def classify_browser_failure(
    exc: BaseException,
    hostname: str | None,
    target: "SpiderTarget | None" = None,
    *,
    used_proxy: bool = False,
) -> ScrapeErrorCode:
    """Single-attempt browser failure classification (R3/R4/R6/R7,
    `contracts/browser-spider.md`, `contracts/browser-safety.md`).

    Priority order (US4, T033):

    1. **SSRF/robots -> BLOCKED, first.** Reuses
       :func:`scrape_core.errors.classify_exception` purely as a BLOCKED
       *detector* here (its own non-BLOCKED outputs -- TIMEOUT/DNS_ERROR/
       PROXY_FAILED/UNKNOWN_ERROR -- are discarded; this function's own
       browser-specific classification below is authoritative for those).
       It recognizes two rejections, neither of them re-implemented here:
       ``scrape_core.safety.middleware.SsrfRejectedError`` (pre-fetch
       scheme/userinfo guard) and ``scrape_core.robots.RobotsBlockedError``
       both carry an explicit ``error_code=BLOCKED`` attribute reaching
       ``errback`` intact (both are raised by ordinary Scrapy downloader
       middlewares, never discarded en route). The per-navigation-hop
       ``PLAYWRIGHT_ABORT_REQUEST`` rejection (`scrape_core.browser.ssrf`)
       is different: aborting inside Chromium's network layer surfaces
       only a generic Playwright network error with no `error_code` of
       its own, so that rejection is recognized instead via
       `classify_exception`'s `hostname`-keyed
       `scrape_core.safety.rejection_registry` side-channel -- the exact
       mechanism `abort_unsafe_request` marks a hostname through (see
       that module's docstring).
    2. **Variant codes (US3, T027)**, checked next: a
       :class:`~scrape_core.browser.variant.VariantConfigError` ->
       ``SELECTOR_BROKEN`` (defensive only -- the spider's pre-fetch guard
       already catches every config error before any request exists); a
       run-time missing/uninteractable variant element (message mentions
       one of `target`'s own selectors, :func:`_variant_selectors`) ->
       ``VARIANT_NOT_FOUND``.
    3. **Proxy-context failure (US4, T032/T033)**: `used_proxy` is `True`
       (the failed request's context was `f"proxy:{provider_id}"`) and the
       exception looks proxy-shaped (:func:`_looks_like_proxy_failure`) ->
       ``PROXY_FAILED`` -- covers both a context-creation-time failure
       (bad/unreachable proxy) and a proxy CONNECT/tunnel refusal at fetch
       time; never a silent direct fetch (`contracts/browser-safety.md`
       "Proxy").
    4. **Catch-all**: :func:`~scrape_core.errors.classify_playwright_exception`
       (Playwright ``TimeoutError`` -> ``TIMEOUT``, else
       ``PLAYWRIGHT_FAILED``) -- unchanged US1 behavior for every target
       that isn't SSRF/robots-blocked, variant-related, or proxy-context-
       shaped.
    """
    if classify_exception(exc, hostname=hostname) == ScrapeErrorCode.BLOCKED:
        return ScrapeErrorCode.BLOCKED

    if isinstance(exc, VariantConfigError):
        return ScrapeErrorCode.SELECTOR_BROKEN
    if target is not None and target.variant_selector_config is not None:
        selectors = _variant_selectors(target)
        if selectors and any(selector in str(exc) for selector in selectors):
            return ScrapeErrorCode.VARIANT_NOT_FOUND

    if used_proxy and _looks_like_proxy_failure(exc):
        return ScrapeErrorCode.PROXY_FAILED

    return classify_playwright_exception(exc)


class GenericBrowserPriceSpider(scrapy.Spider):
    """Render each browser-mode target in Chromium, extract, validate, persist."""

    name = "generic_browser_price_spider"

    def __init__(
        self,
        workspace_id: str | None = None,
        scrape_job_id: str | None = None,
        match_ids: Any = None,
        mode: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not workspace_id:
            raise ValueError("generic_browser_price_spider requires a workspace_id argument")
        parsed_match_ids = _parse_match_ids(match_ids)
        if not parsed_match_ids:
            raise ValueError("generic_browser_price_spider requires a non-empty match_ids argument")

        self.workspace_id: uuid.UUID = uuid.UUID(str(workspace_id))
        self.scrape_job_id: uuid.UUID | None = uuid.UUID(str(scrape_job_id)) if scrape_job_id else None
        self.match_ids: list[uuid.UUID] = parsed_match_ids
        self.mode: str = mode or _DEFAULT_MODE
        self._targets_by_match_id: dict[uuid.UUID, SpiderTarget] = {}
        self._requeue_state_by_match_id: dict[uuid.UUID, _RequeueState] = {}
        # Populated by `start()` from `load_targets`'s bounded-load result --
        # shared workspace-wide provider state; not consumed for context
        # building until the proxied-context branch lands (US4/T032).
        self._visible_providers: VisibleProviders = {}
        self._provider_rows: dict[uuid.UUID, ProxyProvider] = {}
        self._provider_passwords: dict[uuid.UUID, str | None] = {}

    def _admission_context(self) -> AdmissionContext:
        """The small bundle :func:`~scrape_core.targets.dispatch_admission`
        needs -- identical shape to the HTTP spider's (SPEC-14 T006), so
        admission behavior never forks between transports."""
        return AdmissionContext(
            workspace_id=self.workspace_id,
            scrape_job_id=self.scrape_job_id,
            requeue_state_by_match_id=self._requeue_state_by_match_id,
        )

    async def start(self) -> AsyncIterator[scrapy.Request]:
        loaded = await run_in_thread(load_targets, self.workspace_id, self.match_ids)
        self._visible_providers = loaded.visible_providers
        self._provider_rows = loaded.provider_rows
        self._provider_passwords = loaded.provider_passwords

        for target in loaded.targets:
            self._targets_by_match_id[target.match_id] = target
            self._requeue_state_by_match_id[target.match_id] = _RequeueState()

            # US3 (T027) pre-fetch variant-config guard: a malformed/
            # unresolvable `variant_selector_config` is a config error,
            # never a fetch failure -- surface it as a terminal
            # `SELECTOR_BROKEN` result *before* any admission/dispatch
            # (never fetched, `contracts/variant-selection.md`), exactly
            # like the `_DispatchDecision.skip_error_code` "decided but
            # never dispatched" shape just below.
            #
            # Two sources, both caught here so nothing downstream ever
            # sees a raised `VariantConfigError` after a lock/slot is
            # held: (1) `target.variant_config_error` -- an unresolvable
            # `value_from` `load_targets` (T025) already caught
            # off-reactor for this target; (2) a purely structural
            # config error (bad `version`/forbidden action type/missing
            # required key) that `resolve_variant_values` never checks --
            # caught here by proactively building this target's page
            # methods (pure, no I/O) via
            # `scrape_core.browser.page.build_page_methods`, which
            # translates `variant_selector_config` via
            # `parse_variant_config`. `_browser_request_for` rebuilds the
            # identical (by-then side-effect-free) list once dispatched.
            variant_error_message = target.variant_config_error
            if variant_error_message is None and target.variant_selector_config is not None:
                try:
                    build_page_methods(target)
                except VariantConfigError as exc:
                    variant_error_message = str(exc)
            if variant_error_message is not None:
                yield self._build_result(
                    target,
                    target.url,
                    datetime.now(UTC),
                    status_code=None,
                    success=False,
                    error_code=ScrapeErrorCode.SELECTOR_BROKEN,
                    error_message=variant_error_message,
                    access_method=AccessMethod.PLAYWRIGHT_PROXY,
                    attempt_number=1,
                )
                continue

            decision = await run_in_thread(
                _prepare_dispatch, target, 1, self._visible_providers, self._provider_rows
            )
            if decision.plan is None:
                if decision.skip_error_code is not None:
                    yield self._build_result(
                        target,
                        target.url,
                        datetime.now(UTC),
                        status_code=None,
                        success=False,
                        error_code=decision.skip_error_code,
                        error_message=f"attempt 1 not dispatched: {decision.skip_error_code}",
                        access_method=decision.attempted_method,
                        attempt_number=1,
                        proxy_provider_id=(
                            decision.attempted_proxy.provider_id if decision.attempted_proxy else None
                        ),
                        proxy_country=(
                            decision.attempted_proxy.country if decision.attempted_proxy else None
                        ),
                    )
                # else: NONE_RESOLVED access policy -- skip silently, see
                # `_DispatchDecision` docstring (exactly as HTTP).
                continue

            # R7: this spider only ever dispatches a browser fetch --
            # override the shared decision's HTTP-shaped access_method to
            # PLAYWRIGHT_PROXY (the admission gate's rate-limit bucket
            # and in-flight match lock are then keyed/TTL'd for the
            # browser mode, `MATCH_LOCK_BROWSER_TTL_SECONDS`), keeping
            # `use_proxy` for the future proxied-context branch (T032).
            browser_plan = AttemptPlan(
                access_method=AccessMethod.PLAYWRIGHT_PROXY, use_proxy=decision.plan.use_proxy
            )
            result = await dispatch_admission(
                self._admission_context(),
                target,
                1,
                browser_plan,
                decision.proxy,
                build_request=self._browser_request_for,
            )
            if result is not None:
                # R4: no `_dispatch` retry loop -- one attempt per target.
                yield result

    def _browser_request_for(
        self,
        target: SpiderTarget,
        attempt_number: int = 1,
        plan: AttemptPlan | None = None,
        proxy_assignment: ProxyAssignment | None = None,
        permission: Permission | None = None,
        lock: LockGrant | None = None,
    ) -> scrapy.Request:
        """Build the single Playwright request for `target`'s one attempt.

        `plan`/`proxy_assignment` default to a plain unproxied
        `PLAYWRIGHT_PROXY` plan (pre-SPEC-14-admission callers, and unit
        tests, may call this with only `target`).

        US4 (T032, `contracts/browser-safety.md` "Proxy"): when
        `proxy_assignment` names a provider present in
        `self._provider_rows` (populated by `start()` from `load_targets`'s
        bounded load, identical to the HTTP spider), the request routes
        through a per-provider Playwright browser context instead of the
        default one -- `meta["playwright_context"] = f"proxy:{provider_id}"`
        + `meta["playwright_context_kwargs"] = {"proxy": {...}}`, never
        `meta["proxy"]` (that key is the HTTP-transport-specific one
        `scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware` reads;
        this project never registers that middleware). The proxy password
        is the **already-decrypted** string `load_targets` stashed on
        `self._provider_passwords` (never decrypted here, never logged).
        `proxy_provider_id`/`proxy_country` are stamped for the reused
        SPEC-10 attempt audit only when `provider` is actually found (exact
        parity with the HTTP spider's `_request_for`) -- an unresolvable/
        dangling provider id (edge case, mirrors the HTTP spider's
        degrade-not-crash convention) leaves the request on the default
        context with no proxy kwargs and null audit fields, same shape as
        an unproxied target. A genuine context-creation failure with a
        *found* provider surfaces at run time as a Playwright error
        `errback`'s `classify_browser_failure` recognizes as `PROXY_FAILED`
        (T033) -- never a silent direct fetch.
        """
        if plan is None:
            plan = AttemptPlan(access_method=AccessMethod.PLAYWRIGHT_PROXY, use_proxy=False)

        settings = get_settings()
        timeout_ms = effective_timeout(target, settings)

        meta: dict[str, Any] = {
            "match_id": target.match_id,
            "download_slot": str(target.match_id),
            "robots_policy": target.robots_policy,
            "access_method": plan.access_method,
            "attempt_number": attempt_number,
            "proxy_provider_id": None,
            "proxy_country": None,
            # SPEC-10-parity: `parse`/`errback` compute `response_time_ms`
            # from this stashed dispatch clock (`_attempt_kwargs_from_meta`).
            "dispatch_monotonic": time.monotonic(),
            "playwright": True,
            # The handler auto-closes the page once the response/failure
            # is produced -- no leaked page (browser-spider.md).
            "playwright_include_page": False,
            "playwright_page_methods": build_page_methods(target),
            # Bounds the navigation itself by this target's effective
            # timeout (R10), on top of the process-wide
            # `PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT` default.
            "playwright_page_goto_kwargs": {"timeout": timeout_ms},
        }
        if permission is not None:
            meta["semaphore_key"] = permission.semaphore_key
            meta["semaphore_token"] = permission.semaphore_token
        if lock is not None:
            meta["match_lock_key"] = lock.key
            meta["match_lock_token"] = lock.token

        if proxy_assignment is not None:
            provider = self._provider_rows.get(proxy_assignment.provider_id)
            if provider is not None:
                host, port = _parse_host_port(provider.base_url)
                proxy_kwargs: dict[str, Any] = {"server": f"http://{host}:{port}"}
                if provider.username:
                    # Already decrypted off-reactor by `load_targets`
                    # (never here, never logged) -- see docstring.
                    password = self._provider_passwords.get(proxy_assignment.provider_id) or ""
                    proxy_kwargs["username"] = provider.username
                    proxy_kwargs["password"] = password
                # Per-provider context name (never the shared default
                # context) so concurrent targets on different providers
                # never share a browser context/proxy -- scrapy-playwright
                # keys its context pool by this exact string.
                meta["playwright_context"] = f"proxy:{proxy_assignment.provider_id}"
                meta["playwright_context_kwargs"] = {"proxy": proxy_kwargs}
                meta["proxy_provider_id"] = proxy_assignment.provider_id
                meta["proxy_country"] = proxy_assignment.country

        return scrapy.Request(
            url=target.url,
            callback=self.parse,
            errback=self.errback,
            dont_filter=True,
            meta=meta,
        )

    async def parse(self, response: Response, **kwargs: Any) -> Any:
        """Extract + validate the rendered DOM, reusing the HTTP result path.

        `response.text` is scrapy-playwright's post-JS DOM (the page's
        content after every `playwright_page_methods` step ran) -- never
        the pre-render HTML a plain HTTP fetch would see (US1's whole
        point, FR-003).
        """
        sem_key = response.meta.get("semaphore_key")
        sem_token = response.meta.get("semaphore_token")
        if sem_key and sem_token:
            await release_slot(get_redis_client(), key=sem_key, token=sem_token)

        target = self._targets_by_match_id[response.meta["match_id"]]
        now = datetime.now(UTC)
        attempt_kwargs = _attempt_kwargs_from_meta(response.meta)

        status_error_code = classify_http_status(response.status)
        if status_error_code is not None:
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=status_error_code,
                error_message=f"HTTP {response.status}",
                **attempt_kwargs,
            )
            return

        candidate = extract(
            response.text,
            target.profile,
            preferred_method=(
                target.strategy_start.extraction_method if target.strategy_start is not None else None
            ),
        )
        if candidate is None:
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=PRICE_NOT_FOUND,
                error_message="no extraction strategy matched a price",
                **attempt_kwargs,
            )
            return

        profile_confidence_rules = target.profile.confidence_rules if target.profile else None
        confidence_cfg = resolve_confidence_rules(profile_confidence_rules)
        validation_rules = (target.profile.validation_rules if target.profile else None) or {}
        outcome = validate_candidate(candidate, validation_rules, confidence_cfg)

        if isinstance(outcome, Rejected):
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=outcome.error_code,
                error_message=outcome.message,
                candidate_extras=candidate,
                **attempt_kwargs,
            )
            return

        assert isinstance(outcome, Accepted)
        yield self._build_result(
            target,
            response.url,
            now,
            status_code=response.status,
            success=True,
            comparable=outcome.comparable,
            price=outcome.price,
            candidate_extras=candidate,
            **attempt_kwargs,
        )

    async def errback(self, failure: Any) -> Any:
        """Record the single attempt's failure. No retry (R4) -- stop.

        Unlike the HTTP spider's `errback`, this never re-enters
        `_prepare_dispatch`/`dispatch_admission` for a next attempt: the
        browser node has no HTTP-style escalation ladder, and job-level
        re-scrape handles retry instead (`contracts/browser-spider.md`).
        """
        match_id = failure.request.meta.get("match_id")
        target = self._targets_by_match_id.get(match_id)
        if target is None:
            logger.error(
                "generic_browser_price_spider: fetch failure with no known target: %s", failure
            )
            return

        sem_key = failure.request.meta.get("semaphore_key")
        sem_token = failure.request.meta.get("semaphore_token")
        if sem_key and sem_token:
            await release_slot(get_redis_client(), key=sem_key, token=sem_token)

        now = datetime.now(UTC)
        hostname = urlsplit(failure.request.url).hostname
        # US4 (T033): a proxied-context request's `playwright_context` is
        # always `f"proxy:{provider_id}"` (T032) -- an unproxied/default
        # request never carries that meta key.
        used_proxy = str(failure.request.meta.get("playwright_context", "")).startswith("proxy:")
        error_code = classify_browser_failure(failure.value, hostname, target, used_proxy=used_proxy)
        yield self._build_result(
            target,
            failure.request.url,
            now,
            status_code=None,
            success=False,
            error_code=error_code,
            error_message=str(failure.value),
            **_attempt_kwargs_from_meta(failure.request.meta),
        )
        # R4: single attempt, no retry -- the failed attempt's own row
        # above is the terminal outcome for this target.

    def _build_result(
        self,
        target: SpiderTarget,
        url: str,
        scraped_at: datetime,
        **kwargs: Any,
    ) -> ScrapeResult:
        """Thin wrapper over :func:`scrape_core.result_builder.build_scrape_result`
        (SPEC-14 T007) -- supplies this spider's own `workspace_id`/
        `scrape_job_id`."""
        return build_scrape_result(
            target,
            url,
            scraped_at,
            workspace_id=self.workspace_id,
            scrape_job_id=self.scrape_job_id,
            **kwargs,
        )
