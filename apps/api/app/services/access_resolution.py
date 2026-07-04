"""Cache-driving access-policy resolution orchestrator
(`contracts/policy-resolution.md` "Orchestrator" section, SPEC-10 US2 T025).

Mirrors `apps/api/app/services/profile_resolution.py`: this module loads
the **bounded** inputs from Postgres, drives the pure
`app_shared.access.resolution` chain-walk per distinct
`(competitor_id, domain, url_pattern)` group, and reads/writes the
short-TTL Redis resolution cache. `app_shared.access.resolution` itself
performs no I/O at all (FR-007).

This is an **internal batch API** (mirroring the SPEC-06 orchestrator)
for any internal caller that needs to resolve the effective
`AccessPolicy` for a batch of matches -- deliberately not wired to a new
public `/v1` endpoint.

`matches` are any objects exposing `.id` / `.competitor_id` /
`.url_pattern` / `.competitor_url` -- e.g. `CompetitorProductMatch` rows.
`domain` is **not** re-derived from the URL: it is the referenced
`Competitor.domain` (SPEC-05 §22, "one competitor per domain per
workspace") -- the authoritative registered domain, looked up via one
bounded `IN (...)` query, rather than an ad-hoc `urlsplit(...).hostname`
of one particular matched URL (which could disagree, e.g. on a `www.`
subdomain). The spider
(`apps/scrapers/price_monitor/spiders/generic_price_spider.py`) reads
the exact same `Competitor.domain` field (it already loads `Competitor`
rows for `robots_policy`/profile defaults) so both call sites group on
-- and cache under -- the same key.

Bounded loads per call (never per-match, Principle IV):

1. One `scoped_select(Competitor, ...)` `id IN (...)` query (bounded
   over the batch's distinct competitor ids) for `Competitor.domain`.
2. One `visible_policies_select` `name IN ("default", "global_default")`
   query for the workspace default + global default `AccessPolicy` ids.
3. One `scoped_select(DomainAccessRule, ...)` `competitor_id IN (...)`
   query (same bounded competitor id set) for the enabled domain rules.
4. One `visible_policies_select` `id IN (...)` query over every
   candidate policy id referenced by the batch (workspace default,
   global default, every matched domain rule's `access_policy_id`) to
   build the `visible_ids` set (FR-007's "counts only if visible" rule).

Then one `resolve_effective_policy` walk per distinct group (Redis
`GET`/`SET` around it, TTL = `Settings.ACCESS_RESOLUTION_CACHE_TTL_SECONDS`),
returning one `ResolvedPolicy | NONE_RESOLVED` per match.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app_shared.access.repository import (
    GLOBAL_DEFAULT_POLICY_NAME,
    WORKSPACE_DEFAULT_POLICY_NAME,
    visible_policies_select,
)
from app_shared.access.resolution import (
    ResolutionResult,
    access_resolution_cache_key,
    decode_result,
    encode_result,
    resolve_effective_policy,
    select_domain_rule,
)
from app_shared.config import get_settings
from app_shared.models.access import AccessPolicy, DomainAccessRule
from app_shared.models.competitors_matches import Competitor
from app_shared.repository import scoped_select

#: `(competitor_id, url_pattern)` -- `domain` is a deterministic function
#: of `competitor_id` (via `Competitor.domain`), not an independent axis,
#: so it does not need to be part of the grouping key itself.
GroupKey = tuple[uuid.UUID, str | None]


def _group_key(match: object) -> GroupKey:
    return (match.competitor_id, match.url_pattern)  # type: ignore[attr-defined]


def _group_matches(matches: Iterable[object]) -> dict[GroupKey, list[object]]:
    groups: dict[GroupKey, list[object]] = {}
    for match in matches:
        groups.setdefault(_group_key(match), []).append(match)
    return groups


# --- bounded input loaders ---------------------------------------------------


def _load_competitor_domains(
    session: Session, workspace_id: uuid.UUID, competitor_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """One bounded `IN (...)` read -- `Competitor.domain` is the authoritative domain."""
    id_list = list(dict.fromkeys(competitor_ids))
    if not id_list:
        return {}
    stmt = scoped_select(Competitor, workspace_id).where(Competitor.id.in_(id_list))
    rows = session.execute(stmt).scalars().all()
    return {row.id: row.domain for row in rows}


def _load_default_policy_ids(
    session: Session, workspace_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """One query for the reserved-name workspace + global default policies."""
    stmt = visible_policies_select(workspace_id).where(
        AccessPolicy.name.in_([WORKSPACE_DEFAULT_POLICY_NAME, GLOBAL_DEFAULT_POLICY_NAME])
    )
    rows = session.execute(stmt).scalars().all()
    workspace_default_id: uuid.UUID | None = None
    global_default_id: uuid.UUID | None = None
    for row in rows:
        if row.workspace_id == workspace_id and row.name == WORKSPACE_DEFAULT_POLICY_NAME:
            workspace_default_id = row.id
        elif row.workspace_id is None and row.name == GLOBAL_DEFAULT_POLICY_NAME:
            global_default_id = row.id
    return workspace_default_id, global_default_id


def _load_enabled_domain_rules(
    session: Session, workspace_id: uuid.UUID, competitor_ids: Iterable[uuid.UUID]
) -> list[DomainAccessRule]:
    """One bounded `IN (...)` read over the batch's distinct competitor ids."""
    id_list = list(dict.fromkeys(competitor_ids))
    if not id_list:
        return []
    stmt = scoped_select(DomainAccessRule, workspace_id).where(
        DomainAccessRule.competitor_id.in_(id_list),
        DomainAccessRule.enabled.is_(True),
    )
    return list(session.execute(stmt).scalars().all())


def _build_visible_policy_ids(
    session: Session, workspace_id: uuid.UUID, candidate_ids: Iterable[uuid.UUID | None]
) -> set[uuid.UUID]:
    """One `visible_policies_select` `IN (...)` lookup -- bounded regardless of batch size."""
    non_null_ids = {cid for cid in candidate_ids if cid is not None}
    if not non_null_ids:
        return set()
    stmt = visible_policies_select(workspace_id).where(AccessPolicy.id.in_(non_null_ids))
    rows = session.execute(stmt).scalars().all()
    return {row.id for row in rows}


# --- Redis cache get/set (fail-open on read, best-effort on write) ---------


def _cache_get_result(redis: object, cache_key: str) -> ResolutionResult | None:
    """`None` = cache miss or a Redis read error -- fail-open, re-walk the chain."""
    try:
        cached = redis.get(cache_key)  # type: ignore[attr-defined]
    except Exception:
        return None
    if cached is None:
        return None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8")
    try:
        return decode_result(cached)
    except (ValueError, AttributeError):
        return None


def _cache_set_result(redis: object, cache_key: str, result: ResolutionResult) -> None:
    try:
        ttl_seconds = get_settings().ACCESS_RESOLUTION_CACHE_TTL_SECONDS
        redis.set(cache_key, encode_result(result), ex=ttl_seconds)  # type: ignore[attr-defined]
    except Exception:
        # Best-effort repopulate -- the freshly-resolved value is still
        # returned to the caller for this call even if the write fails.
        pass


# --- orchestrator -------------------------------------------------------------


def resolve_access_policies_for_matches(
    session: Session,
    redis: object,
    workspace_id: uuid.UUID,
    matches: Iterable[object],
) -> dict[uuid.UUID, ResolutionResult]:
    """Resolve the applicable `AccessPolicy` for every match in `matches`.

    Returns `{match.id: ResolvedPolicy | NONE_RESOLVED}`. Bounded DB
    access regardless of match count: the competitor domains, the
    default policy ids, the enabled domain rules, and the visible-policy
    id set are each loaded with exactly one query; the chain is then
    walked once per distinct `(competitor_id, url_pattern)` group
    (Redis-cached, keyed by the resolved `domain`) -- no per-match DB
    access (Principle IV).
    """
    matches_list = list(matches)
    if not matches_list:
        return {}

    groups = _group_matches(matches_list)
    competitor_ids = {competitor_id for competitor_id, _url_pattern in groups}

    competitor_domain_by_id = _load_competitor_domains(session, workspace_id, competitor_ids)
    workspace_default_id, global_default_id = _load_default_policy_ids(session, workspace_id)
    domain_rules = _load_enabled_domain_rules(session, workspace_id, competitor_ids)

    rules_by_competitor: dict[uuid.UUID, list[DomainAccessRule]] = {}
    for rule in domain_rules:
        rules_by_competitor.setdefault(rule.competitor_id, []).append(rule)

    candidate_ids: set[uuid.UUID | None] = {workspace_default_id, global_default_id}
    candidate_ids.update(rule.access_policy_id for rule in domain_rules)
    visible_ids = _build_visible_policy_ids(session, workspace_id, candidate_ids)

    group_results: dict[GroupKey, ResolutionResult] = {}
    for (competitor_id, url_pattern), group in groups.items():
        domain = competitor_domain_by_id.get(competitor_id, "")
        cache_key = access_resolution_cache_key(workspace_id, competitor_id, domain, url_pattern)
        cached_result = _cache_get_result(redis, cache_key)
        if cached_result is not None:
            group_results[(competitor_id, url_pattern)] = cached_result
            continue

        candidate_rules = rules_by_competitor.get(competitor_id, [])
        sample_url = group[0].competitor_url  # type: ignore[attr-defined]
        matched_rule = select_domain_rule(candidate_rules, domain=domain, url=sample_url)
        domain_rule_policy_id = matched_rule.access_policy_id if matched_rule is not None else None

        result = resolve_effective_policy(
            domain_rule_policy_id=domain_rule_policy_id,
            workspace_default_policy_id=workspace_default_id,
            global_default_policy_id=global_default_id,
            visible_ids=visible_ids,
        )
        _cache_set_result(redis, cache_key, result)
        group_results[(competitor_id, url_pattern)] = result

    resolved: dict[uuid.UUID, ResolutionResult] = {}
    for group_key, group in groups.items():
        group_result = group_results[group_key]
        for match in group:
            resolved[match.id] = group_result  # type: ignore[attr-defined]
    return resolved
