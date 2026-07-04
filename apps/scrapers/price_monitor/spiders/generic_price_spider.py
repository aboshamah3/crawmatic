"""``generic_price_spider`` — the SPEC-07 US1 MVP HTTP spider, extended by
SPEC-10 US2 to drive direct-vs-proxy behavior.

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

The profile-resolution helpers below duplicate (rather than import) the
**bounded-load** shape of ``apps/api/app/services/profile_resolution.py``
because ``apps/scrapers`` may depend on ``libs/scrape-core`` +
``libs/shared/app_shared`` only (never on another ``apps/*`` member,
`plan.md` "apps -> libs only") — but they read/write the *exact same*
Redis cache key (``app_shared.profiles.resolution.resolution_cache_key``)
that orchestrator populates, so a warm cache is genuinely reused, not
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
  ``errback``), :func:`_prepare_dispatch` runs the pure
  ``app_shared.access.engine.next_attempt``/``assign_proxy`` decision
  plus the Redis ceiling/cooldown/budget checks (``app_shared.access.
  budget``) **off-reactor** via :func:`scrape_core.db.run_in_thread` —
  never synchronously on the reactor thread. A not-allowed decision
  short-circuits to a terminal :class:`~scrape_core.items.ScrapeResult`
  (``RATE_LIMITED``/``PROXY_FAILED``/``LIMIT_REACHED``) instead of
  dispatching a request.
* ``errback`` is ``async def`` so it can ``await run_in_thread(...)`` for
  the same off-reactor precheck before yielding a retry ``scrapy.Request``
  — Scrapy 2.x + the project's ``AsyncioSelectorReactor`` support
  coroutine callbacks/errbacks natively (no ``twisted.inlineCallbacks``
  needed).
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import scrapy
from scrapy.http import Response
from sqlalchemy import select

from app_shared.access.budget import check_domain_cooldown, check_rate_ceilings, incr_and_check_monthly_budget
from app_shared.access.engine import STOP, AttemptPlan, ProxyAssignment, assign_proxy, next_attempt
from app_shared.access.repository import (
    GLOBAL_DEFAULT_POLICY_NAME,
    WORKSPACE_DEFAULT_POLICY_NAME,
    visible_policies_select,
    visible_providers_select,
)
from app_shared.access.resolution import (
    ResolutionResult as AccessResolutionResult,
    ResolvedPolicy as AccessResolvedPolicy,
    access_resolution_cache_key,
    decode_result as decode_access_result,
    encode_result as encode_access_result,
    resolve_effective_policy,
    select_domain_rule,
)
from app_shared.enums import AccessMethod, AccessStrategy, ProxyProviderStatus, ProxyType, RobotsPolicy, ScrapeErrorCode
from app_shared.models.access import AccessPolicy, DomainAccessRule, ProxyProvider
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.identity import Workspace
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.profiles.repository import GLOBAL_DEFAULT_PROFILE_NAME, profile_visibility_map
from app_shared.profiles.resolution import (
    ResolutionResult,
    ResolvedProfile,
    apply_match_override,
    decode_group_result,
    encode_group_result,
    group_matches,
    resolution_cache_key,
    resolve_group,
)
from app_shared.redis_client import get_redis_client
from app_shared.repository import scoped_select
from app_shared.security.encryption import SecretDecryptionError, decrypt_secret

from scrape_core.db import run_in_thread, workspace_txn
from scrape_core.errors import PRICE_NOT_FOUND, classify_exception, classify_http_status
from scrape_core.extraction.pipeline import extract
from scrape_core.items import ScrapeResult
from scrape_core.validation import Accepted, Rejected, validate_candidate

logger = logging.getLogger(__name__)

_DEFAULT_MODE = "HTTP"

#: `{provider_id: (status, type, country)}` -- the shape `assign_proxy` expects.
VisibleProviders = dict[uuid.UUID, tuple[ProxyProviderStatus, ProxyType, str | None]]


@dataclass(frozen=True)
class SpiderTarget:
    """One match bundled with its resolved scrape profile + access policy.

    ``domain``/``access_policy``/``domain_rule`` default to empty/``None``
    so existing hand-built targets (unit tests predating SPEC-10) keep
    constructing without changes -- see `_request_for`'s matching
    defaults.
    """

    match_id: uuid.UUID
    product_id: uuid.UUID
    product_variant_id: uuid.UUID
    competitor_id: uuid.UUID
    url: str
    profile: ScrapeProfile | None
    robots_policy: RobotsPolicy
    domain: str = ""
    access_policy: AccessPolicy | None = None
    domain_rule: DomainAccessRule | None = None


# --- match_ids arg parsing (contracts/spider-args.md) -----------------------


def _parse_match_ids(raw: Any) -> list[uuid.UUID]:
    """Accept a comma-separated string or a JSON list of UUID strings."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("["):
            values = json.loads(text)
        else:
            values = [part.strip() for part in text.split(",") if part.strip()]
    return [uuid.UUID(str(v)) for v in values]


# --- Redis resolution-cache get/set (value codec shared via app_shared) -----
#
# The encode/decode codec itself (`encode_group_result`/`decode_group_result`)
# lives in `app_shared.profiles.resolution` (SPEC-07 tasks.md T055) — shared
# with `apps/api/app/services/profile_resolution.py`, the SPEC-06
# orchestrator that populates this same cache, so the two can never
# silently drift apart. This module only duplicates the **bounded-load**
# shape (see module docstring) — never this codec.


def _cache_get_group_result(redis: Any, cache_key: str) -> ResolutionResult | None:
    """``None`` = cache miss or a Redis read error — fail-open, re-walk the chain."""
    try:
        cached = redis.get(cache_key)
    except Exception:  # noqa: BLE001 - Redis must never be a hard dependency here
        return None
    if cached is None:
        return None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8")
    try:
        return decode_group_result(cached)
    except (ValueError, AttributeError):
        return None


def _cache_set_group_result(redis: Any, cache_key: str, result: ResolutionResult) -> None:
    try:
        from app_shared.config import get_settings

        ttl_seconds = get_settings().PROFILE_RESOLUTION_CACHE_TTL_SECONDS
        redis.set(cache_key, encode_group_result(result), ex=ttl_seconds)
    except Exception:  # noqa: BLE001 - best-effort repopulate only
        pass


# --- Redis access-resolution-cache get/set (SPEC-10 US2, same duplication shape) --
#
# Reads/writes the exact same cache
# `app_shared.access.resolution.access_resolution_cache_key` that
# `apps/api/app/services/access_resolution.py` populates -- see module
# docstring.


def _cache_get_access_result(redis: Any, cache_key: str) -> AccessResolutionResult | None:
    """``None`` = cache miss or a Redis read error — fail-open, re-walk the chain."""
    try:
        cached = redis.get(cache_key)
    except Exception:  # noqa: BLE001 - Redis must never be a hard dependency here
        return None
    if cached is None:
        return None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8")
    try:
        return decode_access_result(cached)
    except (ValueError, AttributeError):
        return None


def _cache_set_access_result(redis: Any, cache_key: str, result: AccessResolutionResult) -> None:
    try:
        from app_shared.config import get_settings

        ttl_seconds = get_settings().ACCESS_RESOLUTION_CACHE_TTL_SECONDS
        redis.set(cache_key, encode_access_result(result), ex=ttl_seconds)
    except Exception:  # noqa: BLE001 - best-effort repopulate only
        pass


@dataclass(frozen=True)
class _LoadedTargets:
    """The bounded-load result: targets plus the workspace-wide provider state
    every target's `_prepare_dispatch` call needs (shared, not duplicated
    per target -- see `load_targets` docstring)."""

    targets: list[SpiderTarget]
    visible_providers: VisibleProviders = field(default_factory=dict)
    provider_rows: dict[uuid.UUID, ProxyProvider] = field(default_factory=dict)
    provider_passwords: dict[uuid.UUID, str | None] = field(default_factory=dict)


# --- bounded profile + access-policy resolution + target load (blocking) ---


def load_targets(workspace_id: uuid.UUID, match_ids: list[uuid.UUID]) -> _LoadedTargets:
    """Load the workspace-scoped matches + their resolved scrape profile + access policy.

    **Blocking** — DB + Redis round trips. Must only ever be called
    inside :func:`scrape_core.db.run_in_thread`, never on the reactor
    thread. Bounded regardless of ``len(match_ids)`` (mirrors
    ``apps/api/app/services/profile_resolution.resolve_profiles_for_matches``
    and, for the SPEC-10 access-policy half,
    ``apps/api/app/services/access_resolution.resolve_access_policies_for_matches``):
    one query for matches, one for the workspace default profile, one
    ``IN`` query for competitor defaults/domains/robots-policy, one for
    the global default profile, one ``IN`` query for profile visibility,
    one for the workspace+global default *access policies*, one ``IN``
    query for enabled domain rules, one ``IN`` query for access-policy
    visibility, one ``IN`` load of the resolved access-policy rows
    themselves, and one load of every workspace-visible ``ProxyProvider``
    — then one Redis-cached chain walk per distinct
    ``(competitor_id, url_pattern)`` group (never per match) for each of
    the two resolution chains.

    Every visible provider's password is decrypted **once here**
    (off-reactor) so `_prepare_dispatch`/`_request_for` (which may run
    on the reactor thread) never call :func:`decrypt_secret` themselves
    and the plaintext is never logged.

    A ``match_id`` not found in ``workspace_id`` is simply absent from
    the result (no cross-read, FR-002).
    """
    if not match_ids:
        return _LoadedTargets(targets=[])

    with workspace_txn(workspace_id) as session:
        matches = (
            session.execute(
                scoped_select(CompetitorProductMatch, workspace_id).where(
                    CompetitorProductMatch.id.in_(match_ids)
                )
            )
            .scalars()
            .all()
        )
        if not matches:
            return _LoadedTargets(targets=[])

        groups = group_matches(matches)
        competitor_ids = {competitor_id for competitor_id, _url_pattern in groups}

        workspace = session.get(Workspace, workspace_id)
        workspace_default_id = workspace.default_scrape_profile_id if workspace else None

        competitor_rows = (
            session.execute(scoped_select(Competitor, workspace_id).where(Competitor.id.in_(competitor_ids)))
            .scalars()
            .all()
        )
        competitor_default_by_id = {row.id: row.default_scrape_profile_id for row in competitor_rows}
        competitor_robots_policy_by_id = {row.id: row.robots_policy for row in competitor_rows}
        competitor_domain_by_id = {row.id: row.domain for row in competitor_rows}

        global_default_id = session.execute(
            select(ScrapeProfile.id).where(
                ScrapeProfile.workspace_id.is_(None),
                ScrapeProfile.name == GLOBAL_DEFAULT_PROFILE_NAME,
            )
        ).scalar_one_or_none()

        candidate_ids: set[uuid.UUID] = {
            cid
            for cid in (
                workspace_default_id,
                global_default_id,
                *competitor_default_by_id.values(),
                *(m.scrape_profile_id for m in matches),
            )
            if cid is not None
        }
        visibility = profile_visibility_map(session, workspace_id, candidate_ids) if candidate_ids else {}
        visible_ids = set(visibility.keys())

        redis = get_redis_client()
        group_results: dict[tuple[uuid.UUID, str], ResolutionResult] = {}
        for (competitor_id, url_pattern), _group in groups.items():
            cache_key = resolution_cache_key(workspace_id, competitor_id, url_pattern)
            cached_result = _cache_get_group_result(redis, cache_key)
            if cached_result is not None:
                group_results[(competitor_id, url_pattern)] = cached_result
                continue
            result = resolve_group(
                competitor_default_id=competitor_default_by_id.get(competitor_id),
                workspace_default_id=workspace_default_id,
                global_default_id=global_default_id,
                visible_ids=visible_ids,
            )
            _cache_set_group_result(redis, cache_key, result)
            group_results[(competitor_id, url_pattern)] = result

        resolved_profile_id_by_match: dict[uuid.UUID, uuid.UUID | None] = {}
        for (competitor_id, url_pattern), group in groups.items():
            group_result = group_results[(competitor_id, url_pattern)]
            for match in group:
                match_result = apply_match_override(group_result, match.scrape_profile_id, visible_ids)
                resolved_profile_id_by_match[match.id] = (
                    match_result.profile_id if isinstance(match_result, ResolvedProfile) else None
                )

        resolved_ids = {pid for pid in resolved_profile_id_by_match.values() if pid is not None}
        profiles_by_id: dict[uuid.UUID, ScrapeProfile] = {}
        if resolved_ids:
            rows = session.execute(select(ScrapeProfile).where(ScrapeProfile.id.in_(resolved_ids))).scalars().all()
            profiles_by_id = {row.id: row for row in rows}

        # --- SPEC-10 US2: effective access-policy resolution -----------------
        # Duplicates the bounded-load shape of
        # `apps/api/app/services/access_resolution.py` (apps -> libs only) but
        # reads/writes the exact same Redis cache key.

        access_workspace_default_id, access_global_default_id = None, None
        default_policy_rows = (
            session.execute(
                visible_policies_select(workspace_id).where(
                    AccessPolicy.name.in_([WORKSPACE_DEFAULT_POLICY_NAME, GLOBAL_DEFAULT_POLICY_NAME])
                )
            )
            .scalars()
            .all()
        )
        for row in default_policy_rows:
            if row.workspace_id == workspace_id and row.name == WORKSPACE_DEFAULT_POLICY_NAME:
                access_workspace_default_id = row.id
            elif row.workspace_id is None and row.name == GLOBAL_DEFAULT_POLICY_NAME:
                access_global_default_id = row.id

        domain_rules = (
            session.execute(
                scoped_select(DomainAccessRule, workspace_id).where(
                    DomainAccessRule.competitor_id.in_(competitor_ids),
                    DomainAccessRule.enabled.is_(True),
                )
            )
            .scalars()
            .all()
        )
        domain_rules_by_competitor: dict[uuid.UUID, list[DomainAccessRule]] = {}
        for rule in domain_rules:
            domain_rules_by_competitor.setdefault(rule.competitor_id, []).append(rule)

        access_candidate_ids: set[uuid.UUID | None] = {access_workspace_default_id, access_global_default_id}
        access_candidate_ids.update(rule.access_policy_id for rule in domain_rules)
        access_non_null_ids = {cid for cid in access_candidate_ids if cid is not None}
        access_visible_ids: set[uuid.UUID] = set()
        if access_non_null_ids:
            access_visible_ids = {
                row.id
                for row in session.execute(
                    visible_policies_select(workspace_id).where(AccessPolicy.id.in_(access_non_null_ids))
                ).scalars()
            }

        access_group_results: dict[tuple[uuid.UUID, str], AccessResolutionResult] = {}
        matched_domain_rule_by_group: dict[tuple[uuid.UUID, str], DomainAccessRule | None] = {}
        for (competitor_id, url_pattern), group in groups.items():
            domain = competitor_domain_by_id.get(competitor_id, "")
            candidate_rules = domain_rules_by_competitor.get(competitor_id, [])
            sample_url = group[0].competitor_url
            matched_rule = select_domain_rule(candidate_rules, domain=domain, url=sample_url)
            matched_domain_rule_by_group[(competitor_id, url_pattern)] = matched_rule

            cache_key = access_resolution_cache_key(workspace_id, competitor_id, domain, url_pattern)
            cached_access_result = _cache_get_access_result(redis, cache_key)
            if cached_access_result is not None:
                access_group_results[(competitor_id, url_pattern)] = cached_access_result
                continue

            domain_rule_policy_id = matched_rule.access_policy_id if matched_rule is not None else None
            access_result = resolve_effective_policy(
                domain_rule_policy_id=domain_rule_policy_id,
                workspace_default_policy_id=access_workspace_default_id,
                global_default_policy_id=access_global_default_id,
                visible_ids=access_visible_ids,
            )
            _cache_set_access_result(redis, cache_key, access_result)
            access_group_results[(competitor_id, url_pattern)] = access_result

        resolved_policy_id_by_match: dict[uuid.UUID, uuid.UUID | None] = {}
        domain_rule_by_match: dict[uuid.UUID, DomainAccessRule | None] = {}
        for (competitor_id, url_pattern), group in groups.items():
            access_result = access_group_results[(competitor_id, url_pattern)]
            matched_rule = matched_domain_rule_by_group[(competitor_id, url_pattern)]
            for match in group:
                resolved_policy_id_by_match[match.id] = (
                    access_result.policy_id if isinstance(access_result, AccessResolvedPolicy) else None
                )
                domain_rule_by_match[match.id] = matched_rule

        resolved_policy_ids = {pid for pid in resolved_policy_id_by_match.values() if pid is not None}
        access_policies_by_id: dict[uuid.UUID, AccessPolicy] = {}
        if resolved_policy_ids:
            rows = session.execute(
                select(AccessPolicy).where(AccessPolicy.id.in_(resolved_policy_ids))
            ).scalars().all()
            access_policies_by_id = {row.id: row for row in rows}

        # Every workspace-visible provider (own+global) -- bounded, loaded
        # once regardless of batch size, not per resolved policy.
        provider_rows_list = session.execute(visible_providers_select(workspace_id)).scalars().all()
        provider_rows_by_id: dict[uuid.UUID, ProxyProvider] = {row.id: row for row in provider_rows_list}
        visible_providers: VisibleProviders = {
            row.id: (row.status, row.type, row.country_code) for row in provider_rows_list
        }
        provider_passwords: dict[uuid.UUID, str | None] = {}
        for row in provider_rows_list:
            if not row.password_encrypted or row.password_key_version is None:
                provider_passwords[row.id] = None
                continue
            try:
                provider_passwords[row.id] = decrypt_secret(row.password_encrypted, row.password_key_version)
            except SecretDecryptionError:
                # Never crash the run over one undecryptable credential --
                # degrade to "no password" for this provider (its request
                # will go out unauthenticated; the target proxy vendor may
                # still allow IP-based auth, or the fetch will simply fail
                # and get classified PROXY_FAILED downstream).
                logger.warning(
                    "generic_price_spider: could not decrypt password for proxy_provider_id=%s",
                    row.id,
                )
                provider_passwords[row.id] = None

        targets: list[SpiderTarget] = []
        for match in matches:
            profile_id = resolved_profile_id_by_match.get(match.id)
            profile = profiles_by_id.get(profile_id) if profile_id else None
            policy_id = resolved_policy_id_by_match.get(match.id)
            targets.append(
                SpiderTarget(
                    match_id=match.id,
                    product_id=match.product_id,
                    product_variant_id=match.product_variant_id,
                    competitor_id=match.competitor_id,
                    url=match.competitor_url,
                    profile=profile,
                    robots_policy=competitor_robots_policy_by_id.get(
                        match.competitor_id, RobotsPolicy.RESPECT
                    ),
                    domain=competitor_domain_by_id.get(match.competitor_id, ""),
                    access_policy=access_policies_by_id.get(policy_id) if policy_id else None,
                    domain_rule=domain_rule_by_match.get(match.id),
                )
            )
        return _LoadedTargets(
            targets=targets,
            visible_providers=visible_providers,
            provider_rows=provider_rows_by_id,
            provider_passwords=provider_passwords,
        )


# --- SPEC-10 US2: per-attempt dispatch decision (blocking; run_in_thread only) --


@dataclass(frozen=True)
class _DispatchDecision:
    """The outcome of :func:`_prepare_dispatch` for one attempt.

    ``plan is None`` means "do not dispatch a request for this attempt"
    -- either because there is genuinely nothing left to try
    (``skip_error_code`` set, e.g. ``RATE_LIMITED``/``PROXY_FAILED``/
    ``LIMIT_REACHED``) or because the target's access policy never
    resolved at all (``skip_error_code is None`` -- `NONE_RESOLVED`,
    per `contracts/policy-resolution.md`: "target skipped, not scraped
    with an implicit policy"; this phase does not yet log that as a
    `ScrapeResult`, deferred to Phase 5/T034's result-side wiring).
    """

    plan: AttemptPlan | None
    proxy: ProxyAssignment | None
    skip_error_code: ScrapeErrorCode | None = None


def _prepare_dispatch(
    target: SpiderTarget,
    attempt_number: int,
    visible_providers: VisibleProviders,
    provider_rows: dict[uuid.UUID, ProxyProvider],
) -> _DispatchDecision:
    """Decide + Redis-gate the next attempt for `target`. **Blocking** (Redis).

    Must only ever be called inside :func:`scrape_core.db.run_in_thread`
    -- never synchronously on the reactor thread (`contracts/
    spider-integration.md` §2/§3). Runs, in order: the per-domain rate
    ceilings + cooldown gate (always, direct or proxied); the pure
    `next_attempt` transport decision; for a proxied plan, `assign_proxy`
    then the monthly-budget gate, rerouting/stopping on exhaustion or a
    disabled/missing provider (`proxy_budget_exhausted=True` reuse -- see
    `app_shared.access.engine` module docstring judgment call 4).
    """
    policy = target.access_policy
    if policy is None:
        # NONE_RESOLVED (no workspace/global default AccessPolicy seeded,
        # and no matching domain rule) -- skip silently per the
        # resolution contract rather than guessing an implicit policy.
        return _DispatchDecision(plan=None, proxy=None, skip_error_code=None)

    redis = get_redis_client()

    per_minute = (
        target.domain_rule.max_requests_per_minute
        if target.domain_rule is not None
        else policy.max_requests_per_minute
    )
    rate_decision = check_rate_ceilings(
        redis,
        policy_id=policy.id,
        domain=target.domain,
        per_minute=per_minute,
        per_hour=policy.max_requests_per_hour,
        per_day=policy.max_requests_per_day,
    )
    if not rate_decision.allowed:
        return _DispatchDecision(plan=None, proxy=None, skip_error_code=ScrapeErrorCode.RATE_LIMITED)

    cooldown_seconds = target.domain_rule.cooldown_seconds if target.domain_rule is not None else 0
    if not check_domain_cooldown(redis, domain=target.domain, cooldown_seconds=cooldown_seconds):
        return _DispatchDecision(plan=None, proxy=None, skip_error_code=ScrapeErrorCode.RATE_LIMITED)

    def _decide(*, proxy_budget_exhausted: bool = False) -> AttemptPlan | Any:
        return next_attempt(
            policy.strategy,
            attempt_number=attempt_number,
            max_retries=policy.max_retries,
            use_proxy_on_first_attempt=policy.use_proxy_on_first_attempt,
            use_proxy_on_retry=policy.use_proxy_on_retry,
            allow_browser_fallback=policy.allow_browser_fallback,
            proxy_budget_exhausted=proxy_budget_exhausted,
        )

    plan = _decide()
    if plan is STOP:
        return _DispatchDecision(plan=None, proxy=None, skip_error_code=None)

    proxy_assignment: ProxyAssignment | None = None
    if plan.use_proxy:
        proxy_assignment = assign_proxy(
            strategy=policy.strategy,
            policy_provider_id=policy.provider_id,
            policy_country=policy.country_code,
            # DomainAccessRule carries no country_code column in this
            # slice -- always None (documented simplification).
            domain_rule_country=None,
            visible_providers=visible_providers,
            attempt_number=attempt_number,
            rotate_per_request=policy.rotate_per_request,
            sticky_session=policy.sticky_session,
            session_seed=f"{target.competitor_id}:{target.domain}",
        )
        if proxy_assignment is None:
            # No eligible provider (disabled/absent) -- degrade per
            # strategy by reusing the budget-exhausted fallback shape
            # (same "wanted a proxy, can't use one" outcome); STOP here
            # means PROXY_FAILED, not a budget/rate issue.
            plan = _decide(proxy_budget_exhausted=True)
            if plan is STOP:
                return _DispatchDecision(plan=None, proxy=None, skip_error_code=ScrapeErrorCode.PROXY_FAILED)
        else:
            provider = provider_rows.get(proxy_assignment.provider_id)
            limit = provider.monthly_budget_limit if provider is not None else None
            budget_result = incr_and_check_monthly_budget(
                redis, provider_id=proxy_assignment.provider_id, limit=limit, now=datetime.now(UTC)
            )
            if not budget_result.allowed:
                plan = _decide(proxy_budget_exhausted=True)
                if plan is STOP:
                    return _DispatchDecision(plan=None, proxy=None, skip_error_code=ScrapeErrorCode.LIMIT_REACHED)
                proxy_assignment = None  # the fallback plan is guaranteed non-proxy

    return _DispatchDecision(plan=plan, proxy=proxy_assignment)


def _parse_host_port(base_url: str) -> tuple[str, int]:
    """Extract `(host, port)` from a `ProxyProvider.base_url`, defaulting the
    port by scheme when absent (providers are expected to set one explicitly)."""
    parsed = urlsplit(base_url)
    host = parsed.hostname or base_url
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


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
        # Populated by `start()` from `load_targets`'s bounded-load result
        # (SPEC-10 US2) -- shared workspace-wide provider state consulted
        # by `_prepare_dispatch`/`_request_for`, not duplicated per target.
        self._visible_providers: VisibleProviders = {}
        self._provider_rows: dict[uuid.UUID, ProxyProvider] = {}
        self._provider_passwords: dict[uuid.UUID, str | None] = {}

    async def start(self) -> AsyncIterator[scrapy.Request]:
        loaded = await run_in_thread(load_targets, self.workspace_id, self.match_ids)
        self._visible_providers = loaded.visible_providers
        self._provider_rows = loaded.provider_rows
        self._provider_passwords = loaded.provider_passwords

        for target in loaded.targets:
            self._targets_by_match_id[target.match_id] = target
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
                    )
                # else: NONE_RESOLVED access policy -- skip silently, see
                # `_DispatchDecision` docstring.
                continue
            yield self._request_for(target, 1, decision.plan, decision.proxy)

    def _request_for(
        self,
        target: SpiderTarget,
        attempt_number: int = 1,
        plan: AttemptPlan | None = None,
        proxy_assignment: ProxyAssignment | None = None,
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
        }
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

    def parse(self, response: Response, **kwargs: Any) -> Any:
        target = self._targets_by_match_id[response.meta["match_id"]]
        now = datetime.now(UTC)

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
            )
            return

        candidate = extract(response.text, target.profile)
        if candidate is None:
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=PRICE_NOT_FOUND,
                error_message="no extraction strategy matched a price",
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
        )

    async def errback(self, failure: Any) -> Any:
        """Retry per the resolved access policy, or emit the terminal failure.

        ``async def`` (Scrapy 2.x + this project's ``AsyncioSelectorReactor``
        support coroutine errbacks natively) so the off-reactor
        ceiling/cooldown/budget precheck for the *retry* attempt can run
        via :func:`scrape_core.db.run_in_thread` here too -- never
        synchronously on the reactor thread (`contracts/
        spider-integration.md` §3). `next_attempt`'s own `attempt_number`
        bookkeeping (`max_retries` cap, terminal browser-fallback intent,
        `STOP`) is entirely reused via `_prepare_dispatch` -- this method
        only tracks "what attempt number comes next".
        """
        match_id = failure.request.meta.get("match_id")
        target = self._targets_by_match_id.get(match_id)
        if target is None:
            logger.error("generic_price_spider: fetch failure with no known target: %s", failure)
            return

        attempt_number = failure.request.meta.get("attempt_number", 1)
        next_attempt_number = attempt_number + 1

        decision = await run_in_thread(
            _prepare_dispatch, target, next_attempt_number, self._visible_providers, self._provider_rows
        )
        if decision.plan is not None:
            yield self._request_for(target, next_attempt_number, decision.plan, decision.proxy)
            return

        now = datetime.now(UTC)
        if decision.skip_error_code is not None:
            error_code = decision.skip_error_code
        else:
            hostname = urlsplit(failure.request.url).hostname
            error_code = classify_exception(failure.value, hostname=hostname)
        yield self._build_result(
            target,
            failure.request.url,
            now,
            status_code=None,
            success=False,
            error_code=error_code,
            error_message=str(failure.value),
        )

    def _build_result(
        self,
        target: SpiderTarget,
        url: str,
        scraped_at: datetime,
        *,
        status_code: int | None,
        success: bool,
        error_code: Any = None,
        error_message: str | None = None,
        comparable: bool = True,
        price: Decimal | None = None,
        candidate_extras: Any = None,
    ) -> ScrapeResult:
        kwargs: dict[str, Any] = {}
        if candidate_extras is not None:
            kwargs.update(
                currency=candidate_extras.currency,
                stock_status=candidate_extras.stock,
                raw_title=candidate_extras.raw_title,
                extraction_method=candidate_extras.method,
                extraction_confidence=Decimal(str(candidate_extras.confidence)),
                selector_used=candidate_extras.selector_used,
            )
        return ScrapeResult(
            workspace_id=self.workspace_id,
            match_id=target.match_id,
            product_id=target.product_id,
            product_variant_id=target.product_variant_id,
            competitor_id=target.competitor_id,
            scrape_job_id=self.scrape_job_id,
            url=url,
            access_method=AccessMethod.DIRECT_HTTP,
            status_code=status_code,
            scraped_at=scraped_at,
            price=price,
            success=success,
            comparable=comparable,
            error_code=error_code,
            error_message=error_message,
            **kwargs,
        )
