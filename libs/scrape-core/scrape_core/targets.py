"""Transport-agnostic spider machinery shared by every Scrapy project
(Constitution Principle I — an ``apps/*`` member may not import another
``apps/*`` member, so code shared by ``apps/scrapers`` and
``apps/scrapers-browser`` must live here).

``contracts/shared-extraction.md``: this module is a **behavior-preserving
move** out of ``apps/scrapers/price_monitor/spiders/generic_price_spider.py``
— ``SpiderTarget``, ``_LoadedTargets``, ``load_targets``,
``_DispatchDecision``, ``_prepare_dispatch``, ``VisibleProviders``, the
Redis resolution-cache get/set helpers, ``_parse_match_ids``,
``_parse_host_port``, ``_attempt_kwargs_from_meta``, ``_elapsed_ms``, and
``_RequeueState`` moved verbatim (module docstrings below still describe
the original SPEC-07/08/10/11/12 provenance of each piece). The HTTP
spider now imports these instead of defining them; the browser spider
(SPEC-14) imports the identical machinery — neither spider duplicates
this logic.

SPEC-14 T005 additionally extends ``SpiderTarget`` with the browser-
relevant already-loaded fields (``wait_for_selector``/
``browser_timeout_ms``/``variant_selector_config``/
``match_variant_values``) — all defaulted so every existing HTTP
constructor call site (including unit tests) keeps constructing
unchanged. ``load_targets`` populates the three profile-sourced fields
from the already-resolved ``ScrapeProfile`` row (no new query);
``match_variant_values`` stays at its default here — its off-reactor
``resolve_variant_values`` resolution is wired in a later phase
(``scrape_core.browser.variant``, US3) once that module exists.

SPEC-14 T006 additionally extracts the admission machinery
(``_acquire_fetch_permission``/``_overflow_to_dispatch``/the reusable
part of ``_dispatch``) as free functions taking a small
``AdmissionContext`` (``workspace_id``/``scrape_job_id``/
``requeue_state_by_match_id``) instead of a bound spider method, so both
spiders share identical admission behavior without duplicating it. Each
spider's own ``_dispatch``/``_acquire_fetch_permission``/
``_overflow_to_dispatch`` become thin wrappers around these functions,
supplying their own transport-specific request-building callback.

Import policy (shared-extraction.md): only ``app_shared.*`` +
``scrape_core.*`` — never ``apps.*``.
"""

from __future__ import annotations

import json
import logging
import random
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import urlsplit

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
from app_shared.enums import (
    AccessMethod,
    ProxyProviderStatus,
    ProxyType,
    RobotsPolicy,
    ScrapeErrorCode,
    ScrapeTargetStatus,
)
from app_shared.jobs.targets import mark_target
from app_shared.limiter.limits import resolve_limits
from app_shared.messaging import enqueue
from app_shared.models.access import AccessPolicy, DomainAccessRule, ProxyProvider
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.identity import Workspace
from app_shared.models.scrape_profiles import ScrapeProfile
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
from app_shared.strategy.resolution import (
    StrategyStart,
    resolve_or_create_strategy_profile,
    resolve_strategy_start,
)
from app_shared.task_names import SCRAPE_DISPATCH_JOB
from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION, derive_url_pattern

from scrape_core.browser.variant import VariantConfigError, resolve_variant_values
from scrape_core.db import run_in_thread, workspace_txn
from scrape_core.limiter import LockGrant, Permission, acquire_lock, acquire_permission, release_slot
from scrape_core.observability import log_event
from scrape_core.reactor import deferred_delay
from scrape_core.result_builder import build_scrape_result

logger = logging.getLogger(__name__)

__all__ = [
    "VisibleProviders",
    "SpiderTarget",
    "_RequeueState",
    "_LoadedTargets",
    "load_targets",
    "_DispatchDecision",
    "_prepare_dispatch",
    "_parse_match_ids",
    "_parse_host_port",
    "_attempt_kwargs_from_meta",
    "_elapsed_ms",
    "AdmissionContext",
    "acquire_fetch_permission",
    "overflow_to_dispatch",
    "dispatch_admission",
]

#: `{provider_id: (status, type, country)}` -- the shape `assign_proxy` expects.
VisibleProviders = dict[uuid.UUID, tuple[ProxyProviderStatus, ProxyType, str | None]]


@dataclass(frozen=True)
class SpiderTarget:
    """One match bundled with its resolved scrape profile + access policy.

    ``domain``/``access_policy``/``domain_rule`` default to empty/``None``
    so existing hand-built targets (unit tests predating SPEC-10) keep
    constructing without changes -- see `_request_for`'s matching
    defaults.

    SPEC-14 (T005): ``wait_for_selector``/``browser_timeout_ms``/
    ``variant_selector_config`` are the resolved profile's browser-mode
    fields (``None`` when the profile carries none, or when there is no
    resolved profile at all -- identical "absent" shape as every other
    profile-sourced field here); ``match_variant_values`` is a slot for
    the match's resolved ``value_from`` values (populated by
    ``scrape_core.browser.variant.resolve_variant_values`` once that
    module exists, US3) -- unpopulated (empty) until then. All four
    default so every pre-SPEC-14 constructor call site (including every
    existing unit test) keeps constructing unchanged.

    SPEC-14 (T025): ``variant_config_error`` is populated by
    ``load_targets`` (never by a hand-built unit-test target, which
    defaults it to ``None``) when this target's
    ``variant_selector_config`` failed off-reactor ``value_from``
    resolution (:func:`~scrape_core.browser.variant.resolve_variant_values`
    raised ``VariantConfigError``) -- the human-readable message, so the
    browser spider's ``start()`` can emit a terminal ``SELECTOR_BROKEN``
    ``ScrapeResult`` for this target *before* admission/dispatch ever
    runs (never fetched, `contracts/variant-selection.md`), mirroring the
    existing ``_DispatchDecision.skip_error_code`` "decided but never
    dispatched" shape.
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
    # SPEC-12 US2 (contracts/consumption.md, D5/D6): this target's group's
    # `domain_strategy_profiles` row id (always present post-T021 -- the
    # get-or-create seam never leaves a group unresolved) and the learned
    # `(access, extraction)` start `resolve_strategy_start` decided for it,
    # `None` when the profile isn't eligible yet (the default ladder is
    # used unchanged). Both default so existing hand-built targets (unit
    # tests predating SPEC-12) keep constructing without changes.
    domain_strategy_profile_id: uuid.UUID | None = None
    strategy_start: StrategyStart | None = None
    # SPEC-14 (T005): browser-mode fields, resolved from the profile row
    # `load_targets` already loaded -- no new query.
    wait_for_selector: str | None = None
    browser_timeout_ms: int | None = None
    variant_selector_config: dict | None = None
    match_variant_values: dict = field(default_factory=dict)
    # SPEC-14 (T025): the message of the `VariantConfigError`
    # `load_targets` caught while resolving this target's
    # `variant_selector_config` off-reactor, or `None` when resolution
    # succeeded (or there was nothing to resolve).
    variant_config_error: str | None = None


@dataclass
class _RequeueState:
    """SPEC-11 US1 (`contracts/spider-integration.md`) per-target
    in-spider rate-limit backoff bookkeeping — keyed by ``match_id`` on
    the spider instance (mirrors ``_targets_by_match_id``), initialized
    once per fresh target in ``start()`` and accumulated across every
    denial this target's attempts hit (including across SPEC-10 retry
    attempts, T013). SPEC-11 US3 (T027, `contracts/overflow-dispatch.md`)
    checks this state on every denial: once ``requeue_count`` exceeds
    ``REQUEUE_MAX_ATTEMPTS`` or ``cumulative_wait`` exceeds
    ``REQUEUE_MAX_TOTAL_WAIT_SECONDS``, the target overflows back to
    Celery instead of looping again.
    """

    requeue_count: int = 0
    cumulative_wait: float = 0.0


def _mark_target_deferred_rate_limited(
    workspace_id: uuid.UUID,
    scrape_job_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """SPEC-11 US3 (T027, `contracts/overflow-dispatch.md` §3): mark one
    overflowed target ``DEFERRED`` + ``RATE_LIMITED`` in a single
    off-reactor ``workspace_txn`` -- **Blocking** (DB round trip). Must
    only ever be called inside :func:`scrape_core.db.run_in_thread`,
    never on the reactor thread. Reuses the single ``mark_target``
    writer (T026) -- no new persistence path.
    """
    with workspace_txn(workspace_id) as session:
        mark_target(
            session,
            workspace_id=workspace_id,
            scrape_job_id=scrape_job_id,
            match_id=match_id,
            status=ScrapeTargetStatus.DEFERRED,
            error_code=ScrapeErrorCode.RATE_LIMITED,
        )


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

    SPEC-14 (T005): also carries each resolved profile's browser-mode
    fields (``wait_for_selector``/``browser_timeout_ms``/
    ``variant_selector_config``) onto its ``SpiderTarget`` -- straight
    off the already-loaded ``ScrapeProfile`` row, no new query.

    SPEC-14 (T025, US3): when the resolved profile carries a
    ``variant_selector_config``, this also resolves every action's
    ``value_from`` against the match row right here (off-reactor, DB
    session still open) via
    :func:`~scrape_core.browser.variant.resolve_variant_values`, storing
    the result on ``SpiderTarget.match_variant_values``. A
    ``VariantConfigError`` (unresolvable/unknown ``value_from``) never
    aborts the whole bounded load -- it is recorded on that one target's
    ``variant_config_error`` instead, for the spider to surface as a
    terminal ``SELECTOR_BROKEN`` result before dispatch.
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

        # --- SPEC-12 US2: per-group domain-strategy profile get-or-create +
        # learned-start resolution (contracts/consumption.md, D5/D6). One
        # query (or, on a brand-new key, one insert + one enqueue) per
        # distinct (competitor_id, url_pattern) group -- never per match
        # (Principle IV) -- reusing the exact `matched_domain_rule_by_group`
        # this function already computed for access-policy resolution just
        # above, so `domain_access_rules.url_pattern_override` (FR-006) is
        # honored from the very same domain rule, no second lookup. The
        # lookup pattern is that manual override when one matched, else a
        # fresh `derive_url_pattern` at the *current*
        # `URL_PATTERN_ALGORITHM_VERSION` -- never the group's own
        # (possibly stale) stored `competitor_product_matches.url_pattern`,
        # so a version bump can never silently mix patterns (FR-005).
        strategy_profile_id_by_group: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        strategy_start_by_group: dict[tuple[uuid.UUID, str], StrategyStart | None] = {}
        for (competitor_id, url_pattern), group in groups.items():
            domain = competitor_domain_by_id.get(competitor_id, "")
            matched_rule = matched_domain_rule_by_group[(competitor_id, url_pattern)]
            override = matched_rule.url_pattern_override if matched_rule is not None else None
            lookup_pattern = override or derive_url_pattern(group[0].competitor_url)

            strategy_profile = resolve_or_create_strategy_profile(
                session,
                redis,
                workspace_id=workspace_id,
                competitor_id=competitor_id,
                domain=domain,
                url_pattern=lookup_pattern,
            )
            strategy_profile_id_by_group[(competitor_id, url_pattern)] = strategy_profile.id
            learned_start = resolve_strategy_start(
                strategy_profile, algorithm_version=URL_PATTERN_ALGORITHM_VERSION
            )
            strategy_start_by_group[(competitor_id, url_pattern)] = learned_start
            if learned_start is not None:
                # Structured observability event (contracts/api-and-observability.md
                # §Observability, T040, SC-001): the consumption resolver returned a
                # learned start, so this group skips the default escalation ladder.
                logger.info(
                    "targets: strategy_learned_start_used "
                    "profile_id=%s access_method=%s extraction_method=%s",
                    strategy_profile.id,
                    learned_start.access_method.value,
                    learned_start.extraction_method.value
                    if learned_start.extraction_method is not None
                    else None,
                )

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
                    "targets: could not decrypt password for proxy_provider_id=%s",
                    row.id,
                )
                provider_passwords[row.id] = None

        targets: list[SpiderTarget] = []
        for match in matches:
            profile_id = resolved_profile_id_by_match.get(match.id)
            profile = profiles_by_id.get(profile_id) if profile_id else None
            policy_id = resolved_policy_id_by_match.get(match.id)
            strategy_group_key = (match.competitor_id, match.url_pattern)
            variant_selector_config = profile.variant_selector_config if profile is not None else None

            # SPEC-14 (T025, US3): resolve every action's `value_from`
            # against this match **off-reactor**, right here, while the
            # DB session is still open -- `parse_variant_config` (which
            # may run later, at/near request-build time) never touches
            # the match again. A malformed config or an unresolvable
            # `value_from` never crashes the whole bounded load (mirrors
            # the `SecretDecryptionError` degrade-and-continue just
            # above) -- it is recorded on this one target's
            # `variant_config_error` instead, so the spider can emit a
            # terminal `SELECTOR_BROKEN` result for only this target
            # before any admission/dispatch (never fetched).
            match_variant_values: dict[str, str] = {}
            variant_config_error: str | None = None
            if variant_selector_config is not None:
                try:
                    match_variant_values = resolve_variant_values(variant_selector_config, match)
                except VariantConfigError as exc:
                    variant_config_error = str(exc)

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
                    domain_strategy_profile_id=strategy_profile_id_by_group.get(strategy_group_key),
                    strategy_start=strategy_start_by_group.get(strategy_group_key),
                    wait_for_selector=profile.wait_for_selector if profile is not None else None,
                    browser_timeout_ms=profile.browser_timeout_ms if profile is not None else None,
                    variant_selector_config=variant_selector_config,
                    match_variant_values=match_variant_values,
                    variant_config_error=variant_config_error,
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
    with an implicit policy" -- silently skipped, no `ScrapeResult`).

    ``attempted_method``/``attempted_proxy`` (SPEC-10 US3, T034) record
    the transport that was *decided but never dispatched* when
    ``skip_error_code`` is set -- there is no real `scrapy.Request`/
    response for these attempts, so `request.meta` can't supply the
    audit fields the way it does for a dispatched attempt (`parse`/
    `errback` read those from the response/failure instead). Unused
    (left at their defaults) whenever ``skip_error_code is None``, since
    that path never calls `_build_result`.
    """

    plan: AttemptPlan | None
    proxy: ProxyAssignment | None
    skip_error_code: ScrapeErrorCode | None = None
    attempted_method: AccessMethod = AccessMethod.DIRECT_HTTP
    attempted_proxy: ProxyAssignment | None = None


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
        # Gated before any transport decision is even made -- there is no
        # real "attempted method" to report, so this leaves
        # `_DispatchDecision`'s DIRECT_HTTP/None defaults in place.
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
            # SPEC-12 US2 (contracts/consumption.md step 3, D6): a learned
            # start seeds attempt 1's access method only -- `next_attempt`
            # itself only ever consults `preferred_method` when
            # `attempt_number == 1` (see its module docstring judgment call
            # "SPEC-12 forward-compat"), so passing this unconditionally on
            # every call (including retries) is safe: it's simply ignored
            # for `attempt_number > 1`.
            preferred_method=(
                target.strategy_start.access_method if target.strategy_start is not None else None
            ),
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
            # means PROXY_FAILED, not a budget/rate issue. `plan` (still
            # the pre-degrade decision here) was a proxied plan -- record
            # that as the attempted method (no provider was ever
            # assigned, so `attempted_proxy` stays None).
            intended_method = plan.access_method
            plan = _decide(proxy_budget_exhausted=True)
            if plan is STOP:
                return _DispatchDecision(
                    plan=None,
                    proxy=None,
                    skip_error_code=ScrapeErrorCode.PROXY_FAILED,
                    attempted_method=intended_method,
                )
        else:
            provider = provider_rows.get(proxy_assignment.provider_id)
            limit = provider.monthly_budget_limit if provider is not None else None
            budget_result = incr_and_check_monthly_budget(
                redis, provider_id=proxy_assignment.provider_id, limit=limit, now=datetime.now(UTC)
            )
            if not budget_result.allowed:
                # `proxy_assignment` (the provider that hit its budget)
                # is worth recording even though the attempt never
                # dispatched -- it's exactly what US3's audit/tuning
                # goal needs ("which provider is exhausted").
                intended_method = plan.access_method
                intended_proxy = proxy_assignment
                plan = _decide(proxy_budget_exhausted=True)
                if plan is STOP:
                    return _DispatchDecision(
                        plan=None,
                        proxy=None,
                        skip_error_code=ScrapeErrorCode.LIMIT_REACHED,
                        attempted_method=intended_method,
                        attempted_proxy=intended_proxy,
                    )
                proxy_assignment = None  # the fallback plan is guaranteed non-proxy

    return _DispatchDecision(plan=plan, proxy=proxy_assignment)


def _elapsed_ms(dispatch_monotonic: float | None) -> int | None:
    """``response_time_ms`` (SPEC-10 US3, T034) -- elapsed wall time since
    ``_request_for`` stashed ``time.monotonic()`` on dispatch, or ``None``
    when the attempt was never dispatched (no request exists to time)."""
    if dispatch_monotonic is None:
        return None
    return round((time.monotonic() - dispatch_monotonic) * 1000)


def _attempt_kwargs_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """The SPEC-10 US3 (T034) per-attempt audit fields for a *dispatched*
    attempt, read back from ``request.meta``/``response.meta`` (stamped by
    ``_request_for`` at dispatch time) -- the real ``access_method``/
    ``proxy_provider_id``/``proxy_country``/``attempt_number``/
    ``response_time_ms`` for `_build_result`, never a hardcoded default.

    SPEC-11 US2 (T022): also carries ``match_lock_key``/
    ``match_lock_token`` (absent -> ``None``) so a dispatched attempt's
    eventual `ScrapeResult` threads its match lock through to the
    persistence pipeline's release (T023).
    """
    return {
        "access_method": meta.get("access_method", AccessMethod.DIRECT_HTTP),
        "attempt_number": meta.get("attempt_number", 1),
        "proxy_provider_id": meta.get("proxy_provider_id"),
        "proxy_country": meta.get("proxy_country"),
        "response_time_ms": _elapsed_ms(meta.get("dispatch_monotonic")),
        "match_lock_key": meta.get("match_lock_key"),
        "match_lock_token": meta.get("match_lock_token"),
    }


def _parse_host_port(base_url: str) -> tuple[str, int]:
    """Extract `(host, port)` from a `ProxyProvider.base_url`, defaulting the
    port by scheme when absent (providers are expected to set one explicitly)."""
    parsed = urlsplit(base_url)
    host = parsed.hostname or base_url
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


# --- SPEC-14 T006: shared admission machinery (rate/concurrency/match-lock) --


@dataclass
class AdmissionContext:
    """The minimal per-run state :func:`acquire_fetch_permission`/
    :func:`overflow_to_dispatch`/:func:`dispatch_admission` need — shared
    identically by every spider (HTTP, browser) so admission behavior
    never forks between transports (`contracts/shared-extraction.md`
    Note). ``requeue_state_by_match_id`` is the **same dict** a spider
    keeps on itself (``self._requeue_state_by_match_id``) — mutated in
    place here, not copied, so a spider's own bookkeeping stays in sync.
    """

    workspace_id: uuid.UUID
    scrape_job_id: uuid.UUID | None
    requeue_state_by_match_id: dict[uuid.UUID, _RequeueState]


async def acquire_fetch_permission(
    ctx: AdmissionContext, target: SpiderTarget, access_method: AccessMethod
) -> Permission | None:
    """Resolve this target's effective limits and acquire a domain
    token + concurrency slot before it may fetch (SPEC-11 US1,
    `contracts/spider-integration.md` steps 1-2).

    Loops on denial: the backoff delay is
    ``max(wait_hint_seconds, cooldown_seconds) + jitter`` (the
    cooldown is a floor, FR-006), then waits via the non-blocking
    :func:`~scrape_core.reactor.deferred_delay` (never
    ``time.sleep``) before retrying from limit-resolution. This
    target's ``requeue_count``/``cumulative_wait`` (kept on
    ``ctx.requeue_state_by_match_id``, reset only when the target
    was first seen in the spider's ``start()``) are bumped on every
    denial. No semaphore/lock is taken on a denied permission, so there
    is nothing to release on denial.

    SPEC-11 US3 (T027, `contracts/overflow-dispatch.md`): once either
    cap is exceeded after a denial (``requeue_count >
    REQUEUE_MAX_ATTEMPTS`` or ``cumulative_wait >
    REQUEUE_MAX_TOTAL_WAIT_SECONDS``), this does **not** wait/retry
    again -- :func:`overflow_to_dispatch` hands the target back to
    Celery and this returns ``None`` instead of a granted
    :class:`Permission`, signalling the caller to dispatch nothing
    for this target (the Scrapyd slot is freed immediately, SC-003).

    SPEC-11 US4 (T031, `contracts/observability.md`): every denial
    emits exactly one of ``rate_limit.hit`` (the token bucket itself
    denied) or ``semaphore.denied`` (the bucket granted but the
    concurrency slot was full) -- see `Permission.denied_by` -- and
    every backoff this loop actually takes (i.e. every denial that
    does *not* immediately overflow) also emits ``rate_limit.requeue``.
    """
    from app_shared.config import get_settings

    settings = get_settings()
    redis = get_redis_client()
    state = ctx.requeue_state_by_match_id[target.match_id]

    while True:
        limits = resolve_limits(
            domain_rule=target.domain_rule,
            access_policy=target.access_policy,
            settings=settings,
        )
        sem_token = secrets.token_hex(16)
        perm = await acquire_permission(
            redis,
            workspace_id=ctx.workspace_id,
            domain=target.domain,
            access_method=access_method,
            limits=limits,
            settings=settings,
            sem_token=sem_token,
        )
        if perm.granted:
            return perm

        # SPEC-11 US4 (T031, contracts/observability.md): distinguish
        # which gate denied -- `semaphore.denied` (concurrency slot
        # full) vs `rate_limit.hit` (token bucket denied, the
        # semaphore was never touched, Permission.denied_by).
        if perm.denied_by == "semaphore":
            log_event(
                logger,
                "semaphore.denied",
                workspace_id=ctx.workspace_id,
                domain=target.domain,
                access_method=access_method,
            )
        else:
            log_event(
                logger,
                "rate_limit.hit",
                workspace_id=ctx.workspace_id,
                domain=target.domain,
                access_method=access_method,
                wait_hint=perm.wait_hint_seconds,
            )

        delay = max(perm.wait_hint_seconds, limits.cooldown_seconds) + random.uniform(
            settings.RATE_LIMIT_JITTER_MIN_SECONDS, settings.RATE_LIMIT_JITTER_MAX_SECONDS
        )
        state.requeue_count += 1
        state.cumulative_wait += delay

        if (
            state.requeue_count > settings.REQUEUE_MAX_ATTEMPTS
            or state.cumulative_wait > settings.REQUEUE_MAX_TOTAL_WAIT_SECONDS
        ):
            await overflow_to_dispatch(ctx, target, perm, redis=redis)
            return None

        log_event(
            logger,
            "rate_limit.requeue",
            workspace_id=ctx.workspace_id,
            match_id=target.match_id,
            requeue_count=state.requeue_count,
            delay=delay,
        )
        await deferred_delay(delay)


async def overflow_to_dispatch(
    ctx: AdmissionContext, target: SpiderTarget, perm: Permission, *, redis: object
) -> None:
    """SPEC-11 US3 (T027, `contracts/overflow-dispatch.md` §3):
    requeue-cap exceeded for `target` -- release any held semaphore
    slot (defensive no-op: a denied `Permission` never carries a
    `semaphore_key`/`semaphore_token`, since the semaphore is never
    acquired on a denied bucket check), mark the target `DEFERRED` +
    `RATE_LIMITED` in one off-reactor `workspace_txn`
    (:func:`_mark_target_deferred_rate_limited`), then re-dispatch
    the whole job via the existing `scrape_dispatch` Celery producer
    (`app_shared.messaging.enqueue` + `app_shared.task_names`, never
    `apps/workers` -- Constitution Principle I) so a fresh
    `dispatch_job` run picks this (now-DEFERRED) target back up
    (T028) and it re-enters the full lock+limiter gate. No request
    is yielded for this attempt -- the Scrapyd slot is freed
    immediately (SC-003).
    """
    if perm.semaphore_key and perm.semaphore_token:
        await release_slot(redis, key=perm.semaphore_key, token=perm.semaphore_token)

    scrape_job_id = ctx.scrape_job_id
    if scrape_job_id is None:
        # No job context to mark/re-dispatch against -- nothing more
        # can be done for this overflowed target (spider-args.md:
        # `scrape_job_id` is expected on every real Scrapyd-dispatched
        # run; only hand-built unit-test spiders may omit it).
        logger.error(
            "targets: requeue-cap overflow with no scrape_job_id -- "
            "cannot mark DEFERRED or re-dispatch match_id=%s",
            target.match_id,
        )
        return

    await run_in_thread(
        _mark_target_deferred_rate_limited,
        ctx.workspace_id,
        scrape_job_id,
        target.match_id,
    )
    await run_in_thread(
        enqueue,
        SCRAPE_DISPATCH_JOB,
        queue="scrape_dispatch",
        kwargs={"scrape_job_id": str(scrape_job_id), "workspace_id": str(ctx.workspace_id)},
    )
    # SPEC-11 US4 (T031, contracts/observability.md): the requeue cap
    # was exceeded and the target is now DEFERRED + re-dispatched --
    # emitted after both the mark and the re-dispatch enqueue commit,
    # mirroring the order the outcomes actually happen in.
    log_event(
        logger,
        "rate_limit.overflow",
        workspace_id=ctx.workspace_id,
        scrape_job_id=scrape_job_id,
        match_id=target.match_id,
    )


async def dispatch_admission(
    ctx: AdmissionContext,
    target: SpiderTarget,
    attempt_number: int,
    plan: AttemptPlan,
    proxy_assignment: ProxyAssignment | None,
    *,
    build_request: Callable[
        [SpiderTarget, int, AttemptPlan, ProxyAssignment | None, Permission, LockGrant], Any
    ],
) -> Any:
    """SPEC-11 US1+US2+US3 combined dispatch gate (`contracts/spider-integration.md`
    steps 1-4): acquire the domain token + concurrency slot
    (:func:`acquire_fetch_permission`, looping on denial until
    granted or the requeue cap overflows, US3 T027), then the
    in-flight match lock **immediately before fetch** (step 3,
    off-reactor via :func:`~scrape_core.limiter.acquire_lock`).

    Returns ``None`` when :func:`acquire_fetch_permission` overflowed
    (requeue cap exceeded -- the target was already marked `DEFERRED`
    and re-dispatched, US3 T027): nothing further to do, no request/
    result for this attempt.

    On a held lock (``None``): releases the semaphore slot just
    acquired in step 2 (nothing else was taken -- no requeue, FR-011/
    014, US2 AS1) and returns a terminal ``SKIPPED``/
    ``LOCKED_ALREADY_RUNNING`` :class:`~scrape_core.items.ScrapeResult`
    (the existing skip-emission shape SPEC-10 uses for not-dispatched
    attempts) instead of a request -- no fetch. On a grant, calls the
    caller-supplied ``build_request`` (each spider's own
    transport-specific request builder -- ``_request_for`` for HTTP,
    ``_browser_request_for`` for the browser spider, SPEC-14) with both
    the semaphore and match-lock key/token available on the granted
    `Permission`/`LockGrant` so the built request can carry them onto
    its meta for `parse`/`errback` to release the slot on response and
    the persistence pipeline to release the lock after the write
    commits.
    """
    from app_shared.config import get_settings

    perm = await acquire_fetch_permission(ctx, target, plan.access_method)
    if perm is None:
        return None

    redis = get_redis_client()
    lock = await acquire_lock(
        redis,
        workspace_id=ctx.workspace_id,
        match_id=target.match_id,
        mode=plan.access_method,
        settings=get_settings(),
    )
    if lock is None:
        await release_slot(redis, key=perm.semaphore_key, token=perm.semaphore_token)
        # SPEC-11 US4 (T031, contracts/observability.md): the match
        # lock was already held -- this attempt is skipped, no fetch
        # (dedup.skip -- LOCKED_ALREADY_RUNNING).
        log_event(
            logger,
            "dedup.skip",
            workspace_id=ctx.workspace_id,
            match_id=target.match_id,
        )
        return build_scrape_result(
            target,
            target.url,
            datetime.now(UTC),
            workspace_id=ctx.workspace_id,
            scrape_job_id=ctx.scrape_job_id,
            status_code=None,
            success=False,
            error_code=ScrapeErrorCode.LOCKED_ALREADY_RUNNING,
            error_message="match lock already held -- another attempt is in flight",
            access_method=plan.access_method,
            attempt_number=attempt_number,
        )

    return build_request(target, attempt_number, plan, proxy_assignment, perm, lock)
