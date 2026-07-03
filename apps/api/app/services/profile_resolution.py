"""Cache-driving config-resolution orchestrator (`contracts/config-resolution.md`
"Orchestrator" section, SPEC-06 US3 T039).

Lives in `apps/api` (not `app_shared`) precisely so the Redis-driving
concern stays out of the framework-agnostic core: this module loads the
**bounded** inputs from Postgres, drives the pure
`app_shared.profiles.resolution` chain-walk per distinct
`(competitor_id, url_pattern)` group, and reads/writes the short-TTL
Redis resolution cache. `app_shared.profiles.resolution` itself performs
no I/O at all (FR-018/FR-019).

This is an **internal batch API** for SPEC-07's refresh flow (and any
other future internal caller) — it is deliberately not wired to a new
public `/v1` endpoint or router in this spec.

Bounded loads per call (never per-match, SC-004):

1. One `session.get(Workspace, workspace_id)` for the workspace default.
2. One `IN (...)` query over the distinct `competitor_id`s present in
   the batch for the competitor defaults.
3. One lookup of the reserved global default row
   (`GLOBAL_DEFAULT_PROFILE_NAME`, `workspace_id IS NULL`).
4. One `app_shared.profiles.repository.profile_visibility_map` `IN (...)`
   lookup over every candidate id referenced by the batch (match
   overrides + competitor defaults + workspace default + global
   default) to build the `visible_ids` set consumed by
   `resolve_group`/`apply_match_override` (FR-013/FR-017).

Then one `resolve_group` walk per distinct group (Redis `GET`/`SET`
around it, TTL = `Settings.PROFILE_RESOLUTION_CACHE_TTL_SECONDS`), and
one `apply_match_override` per match reusing the (possibly cached) group
result.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.config import get_settings
from app_shared.models.competitors_matches import Competitor
from app_shared.models.identity import Workspace
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.profiles.repository import (
    GLOBAL_DEFAULT_PROFILE_NAME,
    profile_visibility_map,
)
from app_shared.profiles.resolution import (
    NONE_RESOLVED,
    ResolutionResult,
    ResolvedProfile,
    apply_match_override,
    group_matches,
    resolution_cache_key,
    resolve_group,
)
from app_shared.repository import scoped_select

# The cached-value marker for a group that resolved to NONE_RESOLVED —
# distinct from any real profile id string.
_CACHE_NONE_MARKER = "none"
_CACHE_FIELD_SEP = "|"


# --- bounded input loaders ---------------------------------------------------


def _load_workspace_default_id(
    session: Session, workspace_id: uuid.UUID
) -> uuid.UUID | None:
    """One row read — `Workspace` carries no RLS (tenant root, plain PK)."""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        return None
    return workspace.default_scrape_profile_id


def _load_competitor_default_ids(
    session: Session, workspace_id: uuid.UUID, competitor_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """One bounded `IN (...)` read over the batch's distinct competitor ids."""
    id_list = list(dict.fromkeys(competitor_ids))
    if not id_list:
        return {}
    stmt = scoped_select(Competitor, workspace_id).where(Competitor.id.in_(id_list))
    rows = session.execute(stmt).scalars().all()
    return {row.id: row.default_scrape_profile_id for row in rows}


def _load_global_default_id(session: Session) -> uuid.UUID | None:
    """One lookup of the reserved global-default row (research D6)."""
    stmt = select(ScrapeProfile.id).where(
        ScrapeProfile.workspace_id.is_(None),
        ScrapeProfile.name == GLOBAL_DEFAULT_PROFILE_NAME,
    )
    return session.execute(stmt).scalar_one_or_none()


def _build_visible_ids(
    session: Session,
    workspace_id: uuid.UUID,
    candidate_ids: Iterable[uuid.UUID | None],
) -> set[uuid.UUID]:
    """One `profile_visibility_map` `IN (...)` lookup over every referenced id.

    A candidate present in the map is visible (own+global, FR-013 read
    side); anything absent (dangling/cross-workspace) is simply not a
    member of the returned set, so `resolve_group`/`apply_match_override`
    treat it as unset (FR-017).
    """
    non_null_ids = {cid for cid in candidate_ids if cid is not None}
    if not non_null_ids:
        return set()
    visibility = profile_visibility_map(session, workspace_id, non_null_ids)
    return set(visibility.keys())


# --- Redis cache get/set (fail-open on read, best-effort on write) ---------


def _encode_group_result(result: ResolutionResult) -> str:
    if result is NONE_RESOLVED:
        return _CACHE_NONE_MARKER
    assert isinstance(result, ResolvedProfile)
    return f"{result.profile_id}{_CACHE_FIELD_SEP}{result.level}"


def _decode_group_result(cached: str) -> ResolutionResult:
    if cached == _CACHE_NONE_MARKER:
        return NONE_RESOLVED
    profile_id_str, _, level = cached.partition(_CACHE_FIELD_SEP)
    return ResolvedProfile(profile_id=uuid.UUID(profile_id_str), level=level)  # type: ignore[arg-type]


def _cache_get_group_result(redis: object, cache_key: str) -> ResolutionResult | None:
    """`None` = cache miss or a Redis read error — never treated as a
    security boundary, so any failure just re-walks the chain (fail-open,
    `contracts/config-resolution.md` "Rules")."""
    try:
        cached = redis.get(cache_key)  # type: ignore[attr-defined]
    except Exception:
        return None
    if cached is None:
        return None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8")
    try:
        return _decode_group_result(cached)
    except (ValueError, AttributeError):
        # Corrupt/unexpected cached payload — treat as a miss.
        return None


def _cache_set_group_result(redis: object, cache_key: str, result: ResolutionResult) -> None:
    try:
        ttl_seconds = get_settings().PROFILE_RESOLUTION_CACHE_TTL_SECONDS
        redis.set(cache_key, _encode_group_result(result), ex=ttl_seconds)  # type: ignore[attr-defined]
    except Exception:
        # Best-effort repopulate — the freshly-resolved value is still
        # returned to the caller for this call even if the write fails.
        pass


# --- orchestrator -------------------------------------------------------------


def resolve_profiles_for_matches(
    session: Session,
    redis: object,
    workspace_id: uuid.UUID,
    matches: Iterable[object],
) -> dict[uuid.UUID, ResolutionResult]:
    """Resolve the applicable scrape profile for every match in `matches`.

    `matches` are any objects exposing `.id`, `.competitor_id`,
    `.url_pattern`, and `.scrape_profile_id` (e.g. `CompetitorProductMatch`
    rows). Returns `{match.id: ResolvedProfile | NONE_RESOLVED}`.

    Bounded DB access regardless of match count (SC-004): the workspace
    default, the competitor defaults, the global default, and the
    visible-id set are each loaded with exactly one query; the chain is
    then walked once per distinct `(competitor_id, url_pattern)` group
    (Redis-cached), and the per-match override applied once per match —
    no per-match DB access.
    """
    matches_list = list(matches)
    if not matches_list:
        return {}

    groups = group_matches(matches_list)
    competitor_ids = {competitor_id for competitor_id, _url_pattern in groups}

    workspace_default_id = _load_workspace_default_id(session, workspace_id)
    competitor_default_by_id = _load_competitor_default_ids(session, workspace_id, competitor_ids)
    global_default_id = _load_global_default_id(session)

    candidate_ids: set[uuid.UUID | None] = {workspace_default_id, global_default_id}
    candidate_ids.update(competitor_default_by_id.values())
    candidate_ids.update(match.scrape_profile_id for match in matches_list)
    visible_ids = _build_visible_ids(session, workspace_id, candidate_ids)

    group_results: dict[tuple[uuid.UUID, str], ResolutionResult] = {}
    for (competitor_id, url_pattern), group in groups.items():
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

    resolved: dict[uuid.UUID, ResolutionResult] = {}
    for (competitor_id, url_pattern), group in groups.items():
        group_result = group_results[(competitor_id, url_pattern)]
        for match in group:
            resolved[match.id] = apply_match_override(
                group_result, match.scrape_profile_id, visible_ids
            )
    return resolved


def invalidate_resolution_cache(
    redis: object, workspace_id: uuid.UUID, competitor_id: uuid.UUID | None = None
) -> None:
    """Best-effort prefix delete of cached resolution entries (FR-019).

    Called after any profile/assignment write that could change a
    resolution outcome. `competitor_id=None` invalidates every group
    cached for the workspace; otherwise only that competitor's groups.
    TTL is the backstop if this best-effort delete itself fails (Redis
    errors here are swallowed — never surfaced to the write-path caller).
    """
    competitor_part = str(competitor_id) if competitor_id is not None else "*"
    pattern = f"profres:{workspace_id}:{competitor_part}:*"
    try:
        keys = list(redis.scan_iter(match=pattern))  # type: ignore[attr-defined]
        if keys:
            redis.delete(*keys)  # type: ignore[attr-defined]
    except Exception:
        pass
