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
carries whether an actual upstream proxy should be used for this fetch
(wired in US4/T032 -- accepted but unused by `_browser_request_for` in
US1, which only ever builds a direct/default Playwright context).

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
from scrape_core.db import run_in_thread
from scrape_core.errors import PRICE_NOT_FOUND, classify_http_status, classify_playwright_exception
from scrape_core.extraction.pipeline import extract
from scrape_core.items import ScrapeResult
from scrape_core.limiter import LockGrant, Permission, release_slot
from scrape_core.result_builder import build_scrape_result
from scrape_core.targets import (
    AdmissionContext,
    VisibleProviders,
    _attempt_kwargs_from_meta,
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


def classify_browser_failure(exc: BaseException, hostname: str | None) -> ScrapeErrorCode:
    """Single-attempt browser failure classification (R3/R4, `contracts/browser-spider.md`).

    US1: delegates entirely to :func:`~scrape_core.errors.classify_playwright_exception`
    (Playwright ``TimeoutError`` -> ``TIMEOUT``, else ``PLAYWRIGHT_FAILED``).
    ``hostname`` is accepted now (unused) so later phases can resolve
    SSRF/robots rejections to ``BLOCKED`` first (US4 T033) and variant
    failures to ``SELECTOR_BROKEN``/``VARIANT_NOT_FOUND`` (US3 T027)
    without changing this function's call sites -- a clean extension
    point, per task T018.
    """
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

        US1: direct/default Playwright context only -- the proxied-
        context branch (`playwright_context`/`playwright_context_kwargs`,
        `proxy_provider_id`/`proxy_country` audit fields) is added in
        US4/T032; `proxy_assignment` is accepted (the shared
        `dispatch_admission` `build_request` callback signature) but
        unused here.
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
        error_code = classify_browser_failure(failure.value, hostname)
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
