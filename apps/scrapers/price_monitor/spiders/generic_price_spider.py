"""``generic_price_spider`` — the SPEC-07 US1 MVP HTTP spider, extended by
SPEC-10 US2 to drive direct-vs-proxy behavior and SPEC-10 US3 to log
every attempt (including retries) with its real transport.

Per ``contracts/spider-args.md``: parses ``workspace_id``/
``scrape_job_id``/``match_ids``/``mode`` from Scrapyd ``schedule.json``
kwargs, loads the matching ``competitor_product_matches`` rows scoped
to ``workspace_id`` (a match not in the workspace is simply absent, no
cross-read), resolves each match's scrape profile via the SPEC-06
resolution chain **once per (competitor_id, url_pattern) group** —
consuming the same Redis resolution cache SPEC-06 already populates,
never re-walking the chain per match — issues a request per match using
the resolved access strategy (SPEC-10, see below), and in ``parse`` runs
extraction + validation and yields a
:class:`~scrape_core.items.ScrapeResult` for both success and failure.
The spider stops at persistence: it never computes alerts, variant
price states, or a ``price_analysis`` task (FR-020).

SPEC-14 (`contracts/shared-extraction.md`, Constitution Principle I): the
transport-agnostic machinery this module used to define directly --
``SpiderTarget``, ``load_targets``, ``_prepare_dispatch``, the Redis
resolution-cache helpers, ``_parse_match_ids``/``_parse_host_port``/
``_attempt_kwargs_from_meta``/``_elapsed_ms``/``_RequeueState``, and the
admission machinery (``_acquire_fetch_permission``/
``_overflow_to_dispatch``/the reusable part of ``_dispatch``) -- now
lives in :mod:`scrape_core.targets`, imported here (and re-exported at
this module's top level so every existing call site, including this
module's own test suite, keeps working unchanged) so the browser spider
(``apps/scrapers-browser``) can share the identical machinery without
importing this ``apps/scrapers`` module (an ``apps -> apps`` import is
forbidden). This module now keeps only what is genuinely
HTTP-transport-specific: ``_request_for`` (the Scrapy request builder,
including the ``Proxy-Authorization`` header), and the multi-attempt
``parse``/``errback``/``_dispatch`` ladder -- the latter now thin
wrappers over the shared admission functions. **Behavior preserved** --
guarded by this module's existing unit + integration suite.

The profile-resolution helpers this module's `load_targets` reuses
duplicate (rather than import) the **bounded-load** shape of
``apps/api/app/services/profile_resolution.py`` because ``apps/scrapers``
(via ``libs/scrape-core``) may depend on ``libs/shared/app_shared`` only
(never on another ``apps/*`` member, `plan.md` "apps -> libs only") --
but they read/write the *exact same* Redis cache key
(``app_shared.profiles.resolution.resolution_cache_key``) that
orchestrator populates, so a warm cache is genuinely reused, not
re-derived under a different key.

SPEC-10 US2 (`contracts/spider-integration.md`) extends this same
request-side seam, duplicating the analogous bounded-load shape for
``apps/api/app/services/access_resolution.py`` (same
``apps -> libs``-only constraint, same warm-cache reuse via
``app_shared.access.resolution.access_resolution_cache_key``):

* ``load_targets`` additionally resolves the effective ``AccessPolicy``
  per ``(competitor_id, url_pattern)`` group (domain comes from the
  already-loaded ``Competitor.domain``, not a URL guess), loads the
  matched ``DomainAccessRule`` (if any, for its ceiling/cooldown
  overrides) and the full workspace-visible ``ProxyProvider`` set, and
  decrypts every visible provider's password **once, off-reactor**
  (never inside ``_request_for``, never logged) so the reactor-thread
  request-building code only ever touches an already-decrypted string.
* Before every dispatch (initial in ``start()`` or a retry in
  ``errback``), :func:`~scrape_core.targets._prepare_dispatch` runs the
  pure ``app_shared.access.engine.next_attempt``/``assign_proxy``
  decision plus the Redis ceiling/cooldown/budget checks
  (``app_shared.access.budget``) **off-reactor** via
  :func:`scrape_core.db.run_in_thread` — never synchronously on the
  reactor thread. A not-allowed decision short-circuits to a terminal
  :class:`~scrape_core.items.ScrapeResult` (``RATE_LIMITED``/
  ``PROXY_FAILED``/``LIMIT_REACHED``) instead of dispatching a request.
* ``errback`` is ``async def`` so it can ``await run_in_thread(...)`` for
  the same off-reactor precheck before yielding a retry ``scrapy.Request``
  — Scrapy 2.x + the project's ``AsyncioSelectorReactor`` support
  coroutine callbacks/errbacks natively (no ``twisted.inlineCallbacks``
  needed).

SPEC-10 US3 (`contracts/spider-integration.md` §4, T033-T035) finishes
this same seam's **result** side: every ``ScrapeResult`` `_build_result`
emits now carries the *real* attempt it describes (``access_method``/
``proxy_provider_id``/``proxy_country``/``attempt_number``/
``status_code``/``response_time_ms``/``error_code``) instead of the
SPEC-10-US2-era hardcoded ``DIRECT_HTTP``/attempt 1. ``errback`` emits
one such result for the attempt that just failed *before* deciding
whether to retry, so a policy that retries direct-then-proxy persists
one ``RequestAttempt`` row per attempt (both the failed direct one and
the eventual proxied one) — never collapsing a whole retry chain into a
single terminal row (FR-012/013/015, SC-002).
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import scrapy
from scrapy.http import Response

from app_shared.access.engine import AttemptPlan, ProxyAssignment
from app_shared.enums import AccessMethod
from app_shared.models.access import ProxyProvider
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.redis_client import get_redis_client

from scrape_core.db import run_in_thread
from scrape_core.errors import PRICE_NOT_FOUND, classify_exception, classify_http_status
from scrape_core.extraction.pipeline import extract
from scrape_core.items import ScrapeResult
from scrape_core.limiter import LockGrant, Permission, release_slot
from scrape_core.result_builder import build_scrape_result
from scrape_core.targets import (
    AdmissionContext,
    VisibleProviders,
    _attempt_kwargs_from_meta,
    _DispatchDecision,
    _LoadedTargets,
    _mark_target_deferred_rate_limited,
    _parse_host_port,
    _parse_match_ids,
    _prepare_dispatch,
    _RequeueState,
    SpiderTarget,
    acquire_fetch_permission,
    dispatch_admission,
    load_targets,
    overflow_to_dispatch,
)
from scrape_core.validation import Accepted, Rejected, validate_candidate

logger = logging.getLogger(__name__)

_DEFAULT_MODE = "HTTP"

# Re-exported so every pre-SPEC-14 import site (this module's own test
# suite: `SpiderTarget`/`_RequeueState`/`_prepare_dispatch`/`load_targets`/
# `_attempt_kwargs_from_meta`/`_mark_target_deferred_rate_limited`, and
# unit tests that construct/patch these directly against this module)
# keeps working unchanged -- shared-extraction.md's behavior-preservation
# contract. The names live in `scrape_core.targets` now; nothing below
# redefines them.
__all__ = [
    "GenericPriceSpider",
    "SpiderTarget",
    "VisibleProviders",
    "_LoadedTargets",
    "load_targets",
    "_DispatchDecision",
    "_prepare_dispatch",
    "_RequeueState",
    "_parse_match_ids",
    "_parse_host_port",
    "_attempt_kwargs_from_meta",
    "_mark_target_deferred_rate_limited",
]


class GenericPriceSpider(scrapy.Spider):
    """Fetch each target's product page over ``DIRECT_HTTP``, extract, validate, persist."""

    name = "generic_price_spider"

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
            raise ValueError("generic_price_spider requires a workspace_id argument")
        parsed_match_ids = _parse_match_ids(match_ids)
        if not parsed_match_ids:
            raise ValueError("generic_price_spider requires a non-empty match_ids argument")

        self.workspace_id: uuid.UUID = uuid.UUID(str(workspace_id))
        self.scrape_job_id: uuid.UUID | None = uuid.UUID(str(scrape_job_id)) if scrape_job_id else None
        self.match_ids: list[uuid.UUID] = parsed_match_ids
        # `mode` is reserved/pass-through in this slice -- only "HTTP" is
        # honored (DIRECT_HTTP); other transport modes are later specs
        # (contracts/spider-args.md).
        self.mode: str = mode or _DEFAULT_MODE
        self._targets_by_match_id: dict[uuid.UUID, SpiderTarget] = {}
        # SPEC-11 US1 (contracts/spider-integration.md): per-target
        # rate-limit backoff bookkeeping, reset only when a fresh target
        # is first seen in `start()` -- see `_RequeueState`.
        self._requeue_state_by_match_id: dict[uuid.UUID, _RequeueState] = {}
        # Populated by `start()` from `load_targets`'s bounded-load result
        # (SPEC-10 US2) -- shared workspace-wide provider state consulted
        # by `_prepare_dispatch`/`_request_for`, not duplicated per target.
        self._visible_providers: VisibleProviders = {}
        self._provider_rows: dict[uuid.UUID, ProxyProvider] = {}
        self._provider_passwords: dict[uuid.UUID, str | None] = {}

    def _admission_context(self) -> AdmissionContext:
        """SPEC-14 T006: the small bundle
        :func:`~scrape_core.targets.acquire_fetch_permission`/
        :func:`~scrape_core.targets.overflow_to_dispatch`/
        :func:`~scrape_core.targets.dispatch_admission` need --
        ``requeue_state_by_match_id`` is this spider's own dict (mutated
        in place by the shared functions, never copied), so this
        instance's bookkeeping stays in sync across calls."""
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
            # SPEC-11 US1: fresh per-target backoff bookkeeping (reset
            # only here, on first sight of this target -- see
            # `_RequeueState`).
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
                        proxy_country=(decision.attempted_proxy.country if decision.attempted_proxy else None),
                    )
                # else: NONE_RESOLVED access policy -- skip silently, see
                # `_DispatchDecision` docstring.
                continue
            result = await self._dispatch(target, 1, decision.plan, decision.proxy)
            if result is not None:
                # SPEC-11 US3 (T027): `None` means the requeue cap
                # overflowed and the target was already marked `DEFERRED`
                # + re-dispatched -- nothing to yield for this attempt.
                yield result

    async def _acquire_fetch_permission(
        self, target: SpiderTarget, access_method: AccessMethod
    ) -> Permission | None:
        """Thin wrapper (SPEC-14 T006) over the shared
        :func:`scrape_core.targets.acquire_fetch_permission` -- see that
        function's docstring for the full behavior (domain-token +
        concurrency-slot acquisition, backoff-loop-on-denial, requeue-cap
        overflow). Kept as a spider method so existing call sites/tests
        that drive this directly (``spider._acquire_fetch_permission(...)``)
        keep working unchanged."""
        return await acquire_fetch_permission(self._admission_context(), target, access_method)

    async def _overflow_to_dispatch(self, target: SpiderTarget, perm: Permission, *, redis: object) -> None:
        """Thin wrapper (SPEC-14 T006) over the shared
        :func:`scrape_core.targets.overflow_to_dispatch`."""
        await overflow_to_dispatch(self._admission_context(), target, perm, redis=redis)

    async def _dispatch(
        self,
        target: SpiderTarget,
        attempt_number: int,
        plan: AttemptPlan,
        proxy_assignment: ProxyAssignment | None,
    ) -> "scrapy.Request | ScrapeResult | None":
        """Thin wrapper (SPEC-14 T006) over the shared
        :func:`scrape_core.targets.dispatch_admission` -- supplies this
        spider's own HTTP-transport-specific request builder
        (:meth:`_request_for`) as the ``build_request`` callback, so the
        admission gate (permission + match lock) stays identical to the
        browser spider's while the actual request built differs per
        transport."""
        return await dispatch_admission(
            self._admission_context(),
            target,
            attempt_number,
            plan,
            proxy_assignment,
            build_request=self._request_for,
        )

    def _request_for(
        self,
        target: SpiderTarget,
        attempt_number: int = 1,
        plan: AttemptPlan | None = None,
        proxy_assignment: ProxyAssignment | None = None,
        permission: Permission | None = None,
        lock: LockGrant | None = None,
    ) -> scrapy.Request:
        """Build the request for `target`'s `attempt_number`-th attempt.

        Carries the resolved per-competitor ``robots_policy`` on
        ``request.meta`` (SPEC-07 tasks.md T054, FR-006) so
        ``RobotsPolicyMiddleware.process_request`` honors it instead of
        silently falling through to its conservative ``RESPECT`` default
        for every request.

        `plan`/`proxy_assignment` default to a plain ``DIRECT_HTTP``, no
        proxy (pre-SPEC-10 callers, and unit tests, may call this with
        only `target`). For a proxied plan, `request.meta["proxy"]` is
        set to ``http://{host}:{port}`` (never embedding credentials --
        SSRF-guard-friendly and keeps the secret out of `request.meta`,
        which can end up in logs/stats) and a ``Proxy-Authorization``
        header is built from the provider's `username` + the **already
        decrypted** password stashed by `load_targets` (never decrypted
        here, never logged: `contracts/spider-integration.md` §2).
        `DIRECT_ONLY` (and any plan with `use_proxy=False`) never sets
        `request.meta["proxy"]` at all (SC-001).

        `permission` (SPEC-11 US1, `contracts/spider-integration.md`
        step 4) is the granted :class:`~scrape_core.limiter.Permission`
        from :meth:`_acquire_fetch_permission`; when given, its
        semaphore `key`/`token` are stamped onto `request.meta` so
        `parse`/`errback` can release the slot on fetch completion
        (T014). `None` (pre-SPEC-11 callers, unit tests) leaves those
        meta keys absent -- no release is attempted for such a request.

        `lock` (SPEC-11 US2, `contracts/spider-integration.md` step 4) is
        the granted :class:`~scrape_core.limiter.LockGrant` from
        :meth:`_dispatch`'s :func:`~scrape_core.limiter.acquire_lock`
        call; when given, its `key`/`token` are stamped onto
        `request.meta` so `parse`/`errback` can carry them onto the
        eventual `ScrapeResult` (`match_lock_key`/`match_lock_token`,
        T020) for the persistence pipeline to release after the write
        commits (T023). `None` (pre-SPEC-11 callers, unit tests) leaves
        those meta keys absent -- no release is attempted.
        """
        if plan is None:
            plan = AttemptPlan(access_method=AccessMethod.DIRECT_HTTP, use_proxy=False)

        meta: dict[str, Any] = {
            "match_id": target.match_id,
            "download_slot": str(target.match_id),
            "robots_policy": target.robots_policy,
            "access_method": plan.access_method,
            "attempt_number": attempt_number,
            "proxy_provider_id": None,
            "proxy_country": None,
            # SPEC-10 US3 (T034): stamped so `parse`/`errback` can compute
            # `response_time_ms` for the eventual `ScrapeResult` -- the
            # only per-attempt clock available (no wall-clock start time
            # is otherwise threaded through Scrapy's request/response
            # cycle here).
            "dispatch_monotonic": time.monotonic(),
        }
        if permission is not None:
            # SPEC-11 US1 (T014): threaded through so `parse`/`errback`
            # can release this fetch's concurrency slot as soon as the
            # response/failure returns.
            meta["semaphore_key"] = permission.semaphore_key
            meta["semaphore_token"] = permission.semaphore_token
        if lock is not None:
            # SPEC-11 US2 (T022): threaded through so `parse`/`errback`
            # can carry the match-lock key/token onto the eventual
            # `ScrapeResult` for the persistence pipeline to release
            # after the observation/attempt write commits (T023).
            meta["match_lock_key"] = lock.key
            meta["match_lock_token"] = lock.token
        headers: dict[str, str] = {}

        if proxy_assignment is not None:
            provider = self._provider_rows.get(proxy_assignment.provider_id)
            if provider is not None:
                host, port = _parse_host_port(provider.base_url)
                meta["proxy"] = f"http://{host}:{port}"
                meta["proxy_provider_id"] = proxy_assignment.provider_id
                meta["proxy_country"] = proxy_assignment.country
                if provider.username:
                    password = self._provider_passwords.get(proxy_assignment.provider_id) or ""
                    token = base64.b64encode(f"{provider.username}:{password}".encode("utf-8")).decode("ascii")
                    headers["Proxy-Authorization"] = f"Basic {token}"

        return scrapy.Request(
            url=target.url,
            callback=self.parse,
            errback=self.errback,
            dont_filter=True,
            headers=headers or None,
            meta=meta,
        )

    async def parse(self, response: Response, **kwargs: Any) -> Any:
        # SPEC-11 US1 (T014): the concurrency slot represents an
        # in-flight *fetch* -- release it as soon as the response
        # returns, off-reactor, distinct from the (later, US2) match
        # lock which spans fetch->persist. A request built without a
        # `Permission` (pre-SPEC-11 callers, unit tests) carries no
        # semaphore meta -- nothing to release.
        sem_key = response.meta.get("semaphore_key")
        sem_token = response.meta.get("semaphore_token")
        if sem_key and sem_token:
            await release_slot(get_redis_client(), key=sem_key, token=sem_token)

        target = self._targets_by_match_id[response.meta["match_id"]]
        now = datetime.now(UTC)
        # SPEC-10 US3 (T034): the *actual* attempt's transport/proxy/timing,
        # read from `response.meta` (stamped by `_request_for` at dispatch
        # time) -- never the pre-SPEC-10 hardcoded `DIRECT_HTTP`.
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

        # SPEC-12 US2 (contracts/consumption.md step 3, D6): a learned
        # extraction method is tried first, falling back to the full
        # default order only if it misses -- never a narrower chain.
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
        """Record the attempt that just failed, then retry or stop.

        ``async def`` (Scrapy 2.x + this project's ``AsyncioSelectorReactor``
        support coroutine errbacks natively) so the off-reactor
        ceiling/cooldown/budget precheck for the *retry* attempt can run
        via :func:`scrape_core.db.run_in_thread` here too -- never
        synchronously on the reactor thread (`contracts/
        spider-integration.md` §3). `next_attempt`'s own `attempt_number`
        bookkeeping (`max_retries` cap, terminal browser-fallback intent,
        `STOP`) is entirely reused via `_prepare_dispatch` -- this method
        only tracks "what attempt number comes next".

        SPEC-10 US3 (T034, `contracts/spider-integration.md` Acceptance
        US3-1/SC-002): a retried attempt is its own attempt, not an
        overwrite of a prior one -- every failure this errback observes
        gets its **own** `ScrapeResult` (the real `access_method`/
        `attempt_number`/proxy fields/timing straight from the failed
        request's own `request.meta`, and the real `error_code` via
        `classify_exception`) *before* deciding whether to retry, so a
        `DIRECT_THEN_PROXY` policy's failed direct attempt 1 persists its
        own `RequestAttempt` row in addition to attempt 2's (whether
        attempt 2 dispatches, is itself gated, or the chain stops here).
        """
        match_id = failure.request.meta.get("match_id")
        target = self._targets_by_match_id.get(match_id)
        if target is None:
            logger.error("generic_price_spider: fetch failure with no known target: %s", failure)
            return

        # SPEC-11 US1 (T014): release the failed attempt's concurrency
        # slot before any SPEC-10 retry re-enters -- the retry
        # (below) acquires a brand-new slot via `_acquire_fetch_permission`.
        sem_key = failure.request.meta.get("semaphore_key")
        sem_token = failure.request.meta.get("semaphore_token")
        if sem_key and sem_token:
            await release_slot(get_redis_client(), key=sem_key, token=sem_token)

        now = datetime.now(UTC)
        hostname = urlsplit(failure.request.url).hostname
        failed_error_code = classify_exception(failure.value, hostname=hostname)
        yield self._build_result(
            target,
            failure.request.url,
            now,
            status_code=None,
            success=False,
            error_code=failed_error_code,
            error_message=str(failure.value),
            **_attempt_kwargs_from_meta(failure.request.meta),
        )

        attempt_number = failure.request.meta.get("attempt_number", 1)
        next_attempt_number = attempt_number + 1

        decision = await run_in_thread(
            _prepare_dispatch, target, next_attempt_number, self._visible_providers, self._provider_rows
        )
        if decision.plan is not None:
            result = await self._dispatch(target, next_attempt_number, decision.plan, decision.proxy)
            if result is not None:
                # SPEC-11 US3 (T027): `None` means the requeue cap
                # overflowed and the target was already marked `DEFERRED`
                # + re-dispatched -- nothing to yield for this attempt.
                yield result
            return

        if decision.skip_error_code is None:
            # Genuinely out of retries (`STOP`, no rate/proxy/budget
            # gating involved -- e.g. `max_retries` exhausted) -- the
            # failed attempt's own row above already captures the
            # terminal outcome; nothing further to record.
            return

        # The *next* attempt (`next_attempt_number`) was decided but
        # never dispatched (rate/cooldown/budget-gated) -- its own
        # separate never-dispatched row, same shape as `start()`'s skip
        # branch (there is no request/response for it, so its fields
        # come from the decision itself, not `request.meta`).
        yield self._build_result(
            target,
            failure.request.url,
            now,
            status_code=None,
            success=False,
            error_code=decision.skip_error_code,
            error_message=f"attempt {next_attempt_number} not dispatched: {decision.skip_error_code}",
            access_method=decision.attempted_method,
            attempt_number=next_attempt_number,
            proxy_provider_id=(decision.attempted_proxy.provider_id if decision.attempted_proxy else None),
            proxy_country=(decision.attempted_proxy.country if decision.attempted_proxy else None),
        )

    def _build_result(
        self,
        target: SpiderTarget,
        url: str,
        scraped_at: datetime,
        **kwargs: Any,
    ) -> ScrapeResult:
        """Thin wrapper (SPEC-14 T007) over the shared
        :func:`scrape_core.result_builder.build_scrape_result` -- supplies
        this spider's own ``workspace_id``/``scrape_job_id`` (the free
        function takes them as explicit parameters instead of reading
        ``self.*``). See that function's docstring for the full field
        contract; kept as a spider method so existing call sites/tests
        that drive this directly (``spider._build_result(...)``) keep
        working unchanged."""
        return build_scrape_result(
            target,
            url,
            scraped_at,
            workspace_id=self.workspace_id,
            scrape_job_id=self.scrape_job_id,
            **kwargs,
        )
