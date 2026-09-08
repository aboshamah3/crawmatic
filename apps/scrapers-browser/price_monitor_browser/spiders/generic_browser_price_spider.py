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
from app_shared.enums import AdapterKey, AccessMethod, ScrapeErrorCode
from app_shared.models.access import ProxyProvider
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.redis_client import get_redis_client

from scrape_core.browser.byte_capture import ByteAccumulator
from scrape_core.browser.domain_profile_registry import set_domain_profile, set_domain_transport
from scrape_core.browser.egress_guard import (
    UpstreamProxy,
    get_process_guard,
)
from scrape_core.browser.page import build_page_methods, effective_timeout
from scrape_core.browser.ssrf import clear_subresource_dns_cache
from scrape_core.browser.variant import VariantConfigError
from scrape_core.adapters import (
    AdapterContext,
    AdapterOutcome,
    AdapterResponse,
    get_adapter,
)
from scrape_core.db import await_in_thread
from scrape_core.errors import (
    classify_exception,
    classify_http_status,
    classify_playwright_exception,
)
from scrape_core.items import ScrapeResult
from scrape_core.limiter import LockGrant, Permission, release_fleet_lease, release_slot
from scrape_core.netledger_middleware import is_proxied
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
    prepare_dispatch_with_backoff,
    sticky_proxy_username,
    next_strategy_method,
)
from scrape_core.validation import (
    Accepted,
    Rejected,
    parse_optional_old_price,
    validate_candidate,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODE = "BROWSER"

__all__ = ["GenericBrowserPriceSpider", "classify_browser_failure"]


def _byte_kwargs_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """EPA B6b: the terminal ``main_document_bytes``/``subresource_bytes``
    kwargs for `_build_result`, read back from this attempt's own
    `ByteAccumulator` (stashed on `meta["_byte_accumulator"]` by
    `_browser_request_for`). A meta dict with no accumulator at all (a
    hand-built target/request in a unit test, or any future call site
    that never dispatched through `_browser_request_for`) yields both
    `None` -- `build_scrape_result`'s own "not measured" default -- never
    raises.
    """
    accumulator = meta.get("_byte_accumulator")
    if accumulator is None:
        return {"main_document_bytes": None, "subresource_bytes": None}
    main_document_bytes, subresource_bytes = accumulator.finalize()
    return {"main_document_bytes": main_document_bytes, "subresource_bytes": subresource_bytes}


def _strategy_handoff_kwargs(
    target: SpiderTarget,
    outcome: ScrapeErrorCode,
    *,
    canonical_url: str | None = None,
) -> dict[str, Any]:
    selection = next_strategy_method(target, outcome)
    if selection is None:
        return {}
    return {
        "chain_complete": False,
        "next_strategy_method_id": selection.method.id,
        "canonical_url": canonical_url,
    }


def _adapter_error_code(outcome: AdapterOutcome) -> ScrapeErrorCode:
    if outcome is AdapterOutcome.NOT_LISTED:
        return ScrapeErrorCode.NOT_LISTED
    if outcome is AdapterOutcome.IDENTITY_MISMATCH:
        return ScrapeErrorCode.IDENTITY_MISMATCH
    # EPA B4: an otherwise valid product response whose variant identity
    # could not be resolved (ambiguous, or the identifiers name another
    # product). A FAILURE, deliberately not NOT_LISTED -- the match is
    # flagged NEEDS_REVIEW in the A6 match_audit_classifications sidecar
    # rather than being silently written off as delisted.
    if outcome is AdapterOutcome.IDENTITY_UNRESOLVED:
        return ScrapeErrorCode.IDENTITY_UNRESOLVED
    return ScrapeErrorCode.PRICE_NOT_FOUND


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


#: Sentinel for "no dispatch has happened yet" in
#: `_last_playwright_context` — `None` is a real context name (the default,
#: unproxied context), so it cannot serve as the initial value.
_NO_CONTEXT_YET = object()

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
    classified = classify_exception(exc, hostname=hostname)
    if classified in (ScrapeErrorCode.BLOCKED, ScrapeErrorCode.POLICY_BLOCKED):
        return classified

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
        authorization_id: str | None = None,
        budget_decision_version: str | None = None,
        entitlement_version: str | None = None,
        breaker_decision: str | None = None,
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
        # EPA C4 (READY-005): the C3 grant this whole crawl was
        # authorized under, read by `scrape_core.netledger_middleware`
        # and stamped onto every `network_operations` row it opens (and
        # settled from the same boundary at close). `None` means the
        # dispatcher did not pass one -- the operation is still recorded,
        # with a NULL `authorization_id`, because an unauthorized fetch
        # that happened is a fact worth having, not a row to suppress.
        self.authorization_id: uuid.UUID | None = (
            uuid.UUID(str(authorization_id)) if authorization_id else None
        )
        # EPA Phase C F3: the three DECISION facts behind that grant,
        # read by `scrape_core.netledger_middleware.operation_intent_for`
        # and written to C1's `network_operations` columns of the same
        # names. Plain strings, never parsed here -- they are opaque
        # version tags whose only consumer is an auditor asking "what did
        # the gate decide when it let this fetch happen".
        self.budget_decision_version: str | None = budget_decision_version or None
        self.entitlement_version: str | None = entitlement_version or None
        self.breaker_decision: str | None = breaker_decision or None
        self._targets_by_match_id: dict[uuid.UUID, SpiderTarget] = {}
        self._requeue_state_by_match_id: dict[uuid.UUID, _RequeueState] = {}
        # Populated by `start()` from `load_targets`'s bounded-load result --
        # shared workspace-wide provider state; not consumed for context
        # building until the proxied-context branch lands (US4/T032).
        self._visible_providers: VisibleProviders = {}
        self._provider_rows: dict[uuid.UUID, ProxyProvider] = {}
        self._provider_passwords: dict[uuid.UUID, str | None] = {}
        # READY F01: the `meta["playwright_context"]` name the previous
        # dispatch used, so `_browser_request_for` can spot a browser
        # CONTEXT switch and drop `scrape_core.browser.ssrf`'s per-host
        # sub-resource DNS memo at that boundary (see there). The initial
        # sentinel is deliberately not `None`: `None` is the real name of
        # the default (unproxied) context, and the first dispatch should
        # start from an empty memo either way.
        self._last_playwright_context: Any = _NO_CONTEXT_YET

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
        # F-2 belt-and-braces, same as the HTTP spider: a duplicate run of
        # this job must fetch nothing already terminal for it. Browser
        # scrapes carry proxy + headless cost, so a duplicate here is
        # worth several HTTP ones.
        loaded = await await_in_thread(
            load_targets, self.workspace_id, self.match_ids, scrape_job_id=self.scrape_job_id
        )
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
                configured_browser_method = (
                    target.strategy_start.access_method
                    if target.strategy_start is not None
                    and target.strategy_start.access_method
                    in (AccessMethod.PLAYWRIGHT_DIRECT, AccessMethod.PLAYWRIGHT_PROXY)
                    else AccessMethod.PLAYWRIGHT_DIRECT
                )
                yield self._build_result(
                    target,
                    target.url,
                    datetime.now(UTC),
                    status_code=None,
                    success=False,
                    error_code=ScrapeErrorCode.SELECTOR_BROKEN,
                    error_message=variant_error_message,
                    access_method=configured_browser_method,
                    attempt_number=1,
                )
                continue

            # Backoff-requeue-defer wrapper (2026-08-02): a RATE_LIMITED
            # ceiling denial is retried at the domain's pace and, once the
            # requeue caps are exceeded, handed back DEFERRED -- it never
            # reaches the terminal skip-result branch below (exactly as HTTP).
            decision = await prepare_dispatch_with_backoff(
                self._admission_context(), target, 1, self._visible_providers, self._provider_rows
            )
            if decision is None:
                continue
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

            # Browser-direct and browser-proxy are distinct audited methods.
            configured_access = (
                target.strategy_start.access_method
                if target.strategy_start is not None
                else decision.plan.access_method
            )
            browser_method = (
                configured_access
                if configured_access
                in (AccessMethod.PLAYWRIGHT_DIRECT, AccessMethod.PLAYWRIGHT_PROXY)
                else (
                    AccessMethod.PLAYWRIGHT_PROXY
                    if decision.plan.use_proxy
                    else AccessMethod.PLAYWRIGHT_DIRECT
                )
            )
            browser_plan = AttemptPlan(
                access_method=browser_method,
                use_proxy=browser_method is AccessMethod.PLAYWRIGHT_PROXY,
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
            plan = AttemptPlan(access_method=AccessMethod.PLAYWRIGHT_DIRECT, use_proxy=False)

        adapter_key = (
            target.profile.adapter_key
            if target.profile is not None
            else AdapterKey.PLAYWRIGHT_RENDERED
        )
        adapter_request = get_adapter(adapter_key).build_request(
            AdapterContext.from_target(target)
        )

        # EPA B6b: record this target's resolved profile for its document
        # hostname so `scrape_core.browser.ssrf.abort_unsafe_request`'s
        # sub-resource interception (called by scrapy-playwright with only
        # the bare Playwright request, no scrapy meta -- see
        # `domain_profile_registry`'s module docstring for why this
        # side-channel is the only available seam) sees the real
        # `certified_resources` for this domain instead of always `None`.
        # A target with no resolved profile (`target.profile is None`)
        # records `None` too -- `should_block`'s documented default-policy
        # value, so an unresolved profile still degrades to the plain
        # default blocklist rather than leaving a stale unrelated entry
        # in place.
        set_domain_profile(urlsplit(adapter_request.url).hostname, target.profile)

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
            "adapter_key": adapter_key,
            "adapter_requested_url": adapter_request.url,
        }
        # EPA B6b (live byte capture): a fresh accumulator per attempt
        # (never shared across dispatches -- each attempt gets its own
        # page). `playwright_page_event_handlers` is scrapy-playwright's
        # own seam for `page.on(event, handler)` registration
        # (`scrapy_playwright.handler._attach_page_event_handlers`,
        # called before `page.goto`), so every response Chromium produces
        # for this attempt's page -- main document and every sub-resource
        # that wasn't aborted by the resource-blocking policy -- reaches
        # `byte_accumulator.handle_response`. `parse`/`errback` read the
        # totals back via `_byte_kwargs_from_meta` once the attempt is
        # terminal (`ByteAccumulator.finalize` -- see that class for why
        # "nothing observed" and "observed, zero bytes" are kept distinct).
        byte_accumulator = ByteAccumulator()
        meta["_byte_accumulator"] = byte_accumulator
        meta["playwright_page_event_handlers"] = {"response": byte_accumulator.handle_response}
        # Headless Chromium's default UA ("HeadlessChrome") gets a
        # bot-challenge page from amazon.sa with none of the product
        # markup, so every fetch carries the realistic UA. Set per
        # request rather than via PLAYWRIGHT_CONTEXTS -- see
        # settings.USER_AGENT for why a startup context wedges this node.
        context_kwargs: dict[str, Any] = {}
        user_agent = self.settings.get("USER_AGENT")
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        # READY F01: a service worker outlives the page that registered it
        # and re-issues fetches from its own context, where no
        # `PLAYWRIGHT_ABORT_REQUEST` route hook is attached -- so it is the
        # one browser feature that can egress without passing the route
        # layer at all. Blocked at context creation (`"block"`, the shipped
        # default of `BROWSER_SERVICE_WORKERS`); the setting exists only
        # for a diagnostic run against a site that will not render without
        # one. The connection-time guard still sees such a fetch even when
        # this is set to `"allow"` -- this closes the *route*-layer hole,
        # the guard closes the connection-layer one.
        context_kwargs["service_workers"] = settings.BROWSER_SERVICE_WORKERS
        if permission is not None:
            meta["semaphore_key"] = permission.semaphore_key
            meta["semaphore_token"] = permission.semaphore_token
            # EPA B5 (F10): the FLEET-wide host lease. It is acquired
            # before this request is yielded -- i.e. before scrapy-
            # playwright opens the page and calls `page.goto` -- and
            # released once the page has closed and the response (or
            # failure) reached `parse`/`errback`. Every child resource
            # the navigation pulls rides this ONE lease: the host counts
            # a navigation as one visit, and leasing per subresource
            # would let a single page exhaust the fleet's ceiling for the
            # whole domain.
            meta["fleet_key"] = permission.fleet_key
            meta["fleet_token"] = permission.fleet_token
        if lock is not None:
            meta["match_lock_key"] = lock.key
            meta["match_lock_token"] = lock.token

        if proxy_assignment is not None:
            provider = self._provider_rows.get(proxy_assignment.provider_id)
            if provider is not None:
                host, port = _parse_host_port(provider.base_url)
                upstream_server = f"http://{host}:{port}"
                upstream_username: str | None = None
                upstream_password: str | None = None
                if provider.username:
                    # Already decrypted off-reactor by `load_targets`
                    # (never here, never logged) -- see docstring.
                    upstream_password = (
                        self._provider_passwords.get(proxy_assignment.provider_id) or ""
                    )
                    # Issue 5 parity with the HTTP spider: DataImpulse
                    # sticky sessions ride the username (`;sessid.<id>`).
                    upstream_username = sticky_proxy_username(
                        provider.username, provider.base_url, proxy_assignment.sticky_key
                    )
                # READY F01: a per-context `proxy` REPLACES the launch-level
                # `--proxy-server=http://127.0.0.1:<guard port>`, so a
                # proxied context pointed straight at DataImpulse would take
                # the whole proxied leg back outside the guard -- exactly
                # the leg where a redirect to an internal address matters
                # most. Point the context at a loopback listener the guard
                # binds FOR THIS UPSTREAM instead: the guard holds the real
                # upstream (sticky username included, built above and never
                # rebuilt there) and forwards `CONNECT` on that port only
                # after the same hostname validation. Chromium therefore
                # never sees the provider credential at all.
                #
                # A port and not proxy credentials: Chromium does not send
                # `Proxy-Authorization` preemptively (Playwright answers a
                # `407` challenge, which this guard cannot issue because the
                # same listener also serves unproxied contexts), so a
                # credential-selected leg silently degrades to a DIRECT dial
                # from the fleet IP while the ledger still books PROXY. The
                # accepting socket cannot fail to be presented.
                #
                # `get_process_guard()` (never `ensure_process_guard()`):
                # the question is whether the *settings module* launched
                # Chromium behind a guard. When it did not -- a unit test
                # driving this method directly -- there is no launch
                # argument to override either, so the pre-F01 direct kwargs
                # (straight to the provider, still a real proxied leg) are
                # the correct wiring. Registration itself is never
                # swallowed: if the guard is running and cannot bind the
                # leg, this raises rather than quietly egressing direct.
                guard = get_process_guard()
                if guard is not None:
                    leg_port = guard.register_upstream(
                        UpstreamProxy(
                            server=upstream_server,
                            username=upstream_username,
                            password=upstream_password,
                        )
                    )
                    proxy_kwargs: dict[str, Any] = {
                        "server": f"http://127.0.0.1:{leg_port}"
                    }
                else:
                    proxy_kwargs = {"server": upstream_server}
                    if upstream_username is not None:
                        proxy_kwargs["username"] = upstream_username
                        proxy_kwargs["password"] = upstream_password or ""
                # Per-provider context name (never the shared default
                # context) so concurrent targets on different providers
                # never share a browser context/proxy -- scrapy-playwright
                # keys its context pool by this exact string.
                meta["playwright_context"] = f"proxy:{proxy_assignment.provider_id}"
                context_kwargs["proxy"] = proxy_kwargs
                meta["proxy_provider_id"] = proxy_assignment.provider_id
                meta["proxy_country"] = proxy_assignment.country

        # Applies to the per-provider context when proxied, and to the
        # (lazily created) default context otherwise -- so an unproxied
        # browser fetch carries the realistic UA too.
        if context_kwargs:
            meta["playwright_context_kwargs"] = context_kwargs

        # EPA B5: record whether THIS leg's bytes will cross the paid
        # proxy, keyed by the same document hostname as the profile above,
        # so `scrape_core.browser.ssrf.abort_unsafe_request` can apply the
        # document-only rule to a listed domain's proxied legs only. The
        # predicate is the ledger's own `_is_proxied(meta)` (imported, not
        # re-implemented) so "proxied" can never come to mean one thing to
        # the resource policy and another to the cost ledger -- which is
        # exactly the drift that would make the canary's proxy-bytes
        # measurement disagree with what the ledger bills. Recorded AFTER
        # the proxy-assignment block above, since that is what sets
        # `meta["playwright_context"]`.
        set_domain_transport(
            urlsplit(adapter_request.url).hostname,
            "PROXY" if is_proxied(meta) else "DIRECT",
        )

        # READY F01: `scrape_core.browser.ssrf`'s sub-resource DNS-safety
        # cache is a 60 s per-host memo, and its lifetime must not outlive
        # the browser CONTEXT it was populated in -- a new context is a new
        # (possibly proxied, possibly differently-resolving) network view,
        # and carrying a stale "this host resolves publicly" verdict across
        # that boundary is exactly the rebinding window the memo would
        # otherwise open. scrapy-playwright keys its context pool by
        # `meta["playwright_context"]`, so a change in that name IS a
        # context switch -- and clearing only on a switch keeps the memo
        # doing its job (one lookup per host, not one per sub-resource)
        # inside a context.
        dispatch_context = meta.get("playwright_context")
        if dispatch_context != self._last_playwright_context:
            clear_subresource_dns_cache()
            self._last_playwright_context = dispatch_context

        return scrapy.Request(
            url=adapter_request.url,
            callback=self.parse,
            errback=self.errback,
            dont_filter=True,
            headers=dict(adapter_request.headers) or None,
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
        # EPA B5 (F10): and the FLEET host lease, on this same path -- a
        # request built without one (pre-B5 callers, unit tests) carries
        # no fleet meta and there is nothing to release.
        fleet_key = response.meta.get("fleet_key")
        fleet_token = response.meta.get("fleet_token")
        if fleet_key and fleet_token:
            await release_fleet_lease(get_redis_client(), key=fleet_key, token=fleet_token)

        target = self._targets_by_match_id[response.meta["match_id"]]
        now = datetime.now(UTC)
        attempt_kwargs = _attempt_kwargs_from_meta(response.meta)
        # EPA B6b: the page has already closed by the time `parse` runs
        # (`playwright_include_page: False`), so every response this
        # attempt's page will ever produce has already reached the
        # accumulator -- safe to finalize now.
        attempt_kwargs.update(_byte_kwargs_from_meta(response.meta))

        adapter_key = response.meta.get("adapter_key", AdapterKey.PLAYWRIGHT_RENDERED)
        status_error_code = classify_http_status(response.status)
        if adapter_key is AdapterKey.EXACT_ID_URL_REPAIR and response.status == 404:
            status_error_code = None
        if status_error_code is not None:
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=status_error_code,
                error_message=f"HTTP {response.status}",
                final_url=response.url,
                **_strategy_handoff_kwargs(target, status_error_code),
                **attempt_kwargs,
            )
            return

        adapter_result = get_adapter(adapter_key).adapt(
            AdapterResponse(
                body=response.body,
                final_url=response.url,
                requested_url=response.meta.get("adapter_requested_url", response.request.url),
                status=response.status,
            ),
            AdapterContext.from_target(target),
            preferred_method=(
                target.strategy_start.extraction_method
                if target.strategy_start is not None
                else None
            ),
        )
        candidate = adapter_result.candidate
        if adapter_result.outcome is not AdapterOutcome.FOUND or candidate is None:
            error_code = _adapter_error_code(adapter_result.outcome)
            canonical_url = (
                adapter_result.canonical_url
                if adapter_result.outcome is AdapterOutcome.REPAIRED
                else None
            )
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=error_code,
                error_message=(
                    adapter_result.message
                    or "adapter found no exact, identity-valid price"
                ),
                final_url=adapter_result.final_url,
                identity_validation_result=adapter_result.identity.status.value,
                # EPA B6 (folded-in item 2): carry a B4 adapter's
                # Ambiguous/IdentityIncompatible needs_review flag through
                # to the persistence pipeline's NEEDS_REVIEW sidecar
                # upsert (`adapter_result.metadata` defaults to `{}` for
                # every other outcome, so this is `False` unless the
                # adapter explicitly set it).
                needs_review=bool(adapter_result.metadata.get("needs_review", False)),
                **_strategy_handoff_kwargs(
                    target,
                    error_code,
                    canonical_url=canonical_url,
                ),
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
                final_url=adapter_result.final_url,
                identity_validation_result=adapter_result.identity.status.value,
                **_strategy_handoff_kwargs(target, outcome.error_code),
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
            old_price=parse_optional_old_price(
                adapter_result.old_price_text,
                current_price=outcome.price,
            ),
            candidate_extras=candidate,
            final_url=adapter_result.final_url,
            identity_validation_result=adapter_result.identity.status.value,
            canonical_url=adapter_result.canonical_url,
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
        # EPA B5 (F10): and the FLEET host lease, on this same path -- a
        # request built without one (pre-B5 callers, unit tests) carries
        # no fleet meta and there is nothing to release.
        fleet_key = failure.request.meta.get("fleet_key")
        fleet_token = failure.request.meta.get("fleet_token")
        if fleet_key and fleet_token:
            await release_fleet_lease(get_redis_client(), key=fleet_key, token=fleet_token)

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
            final_url=failure.request.url,
            **_strategy_handoff_kwargs(target, error_code),
            **_attempt_kwargs_from_meta(failure.request.meta),
            # EPA B6b: whatever responses this attempt's page did see
            # before the failure (e.g. a document response the extractor
            # then failed to validate, or a timeout after some
            # sub-resources already loaded) are still real
            # transport-observed bytes -- `ByteAccumulator.finalize`
            # returns `(None, None)` when nothing was ever measured, so a
            # total pre-response failure (DNS/timeout before any bytes)
            # still leaves both columns NULL, never a fabricated 0.
            **_byte_kwargs_from_meta(failure.request.meta),
        )
        # The browser process still performs one fetch.  A configured next
        # method is handed back durably to dispatch rather than retried in
        # this process, so HTTP/browser node boundaries remain explicit.

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
        # The browser spider currently owns a single-attempt chain, so every
        # result it emits is terminal.  Keep the stamp explicit so future
        # browser fallbacks must consciously change the lifecycle contract.
        kwargs.setdefault("chain_complete", True)
        return build_scrape_result(
            target,
            url,
            scraped_at,
            workspace_id=self.workspace_id,
            scrape_job_id=self.scrape_job_id,
            **kwargs,
        )
