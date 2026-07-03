"""Pure resolution core (`contracts/config-resolution.md`, SPEC-06 US3 T036).

Given a match (or a `(competitor_id, url_pattern)` group of matches),
returns the single scrape profile that applies by walking the §9 chain:
match override -> domain-strategy (tolerated no-op, `None` this spec,
FR-015) -> competitor default -> workspace default -> global default. A
candidate id counts only if it is present in the caller-supplied
`visible_ids` set (own+global, from
`app_shared.profiles.repository.visible_profiles_select`); a
dangling/cross-workspace id is treated as unset and the chain falls
through to the next level (FR-017). An explicit :data:`NONE_RESOLVED`
sentinel is returned -- never an error, never an arbitrary row -- when no
level in the chain supplies a visible id (FR-016).

Batch resolution groups matches by `(competitor_id, url_pattern)` so the
chain is walked once per distinct group, not once per match (FR-018,
SC-004) -- the `apps/api/app/services/profile_resolution.py` orchestrator
drives this pure core with the bounded DB loads + the Redis cache; this
module performs **no** I/O (no DB execution, no Redis, no FastAPI).

Pure, framework-agnostic (SQLAlchemy/Redis/FastAPI-free) per the plan's
Constitution I / Principle V import-boundary discipline.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

# The precedence level that supplied a resolved profile id. "domain_strategy"
# is included for forward compatibility with SPEC-12 (its backing table
# doesn't exist yet -- `domain_strategy_id` is always `None` in this spec,
# so this level is never actually produced today, FR-015).
ResolutionLevel = Literal["match", "domain_strategy", "competitor", "workspace", "global"]


@dataclass(frozen=True)
class ResolvedProfile:
    """A resolved scrape-profile id plus the precedence level that supplied it."""

    profile_id: uuid.UUID
    level: ResolutionLevel


class _NoneResolved:
    """Singleton sentinel: no level in the chain supplied a visible profile (FR-016).

    A distinct, explicit result type -- not an error, not an arbitrary
    row, and not confusable with a real :class:`ResolvedProfile`.
    """

    _instance: "_NoneResolved | None" = None

    def __new__(cls) -> "_NoneResolved":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "NONE_RESOLVED"

    def __bool__(self) -> bool:
        return False


#: Explicit "no profile resolved" marker (FR-016). Compare with `is`.
NONE_RESOLVED = _NoneResolved()

#: The shape returned by :func:`resolve_group` / :func:`apply_match_override`.
ResolutionResult = ResolvedProfile | _NoneResolved


def group_key(match: object) -> tuple[uuid.UUID, str]:
    """`(competitor_id, url_pattern)` -- the batch-resolution grouping key (FR-018).

    `match` is any object exposing `.competitor_id` / `.url_pattern`
    attributes (e.g. an ORM `CompetitorProductMatch` row or a plain
    namedtuple/dataclass in tests) -- this module never touches the DB.
    """
    return (match.competitor_id, match.url_pattern)


def group_matches(matches: Iterable[object]) -> dict[tuple[uuid.UUID, str], list[object]]:
    """Bucket `matches` by :func:`group_key` -- one entry per distinct group (FR-018).

    Preserves first-seen group order and per-group insertion order (not
    load-bearing, just deterministic for tests).
    """
    groups: dict[tuple[uuid.UUID, str], list[object]] = {}
    for match in matches:
        groups.setdefault(group_key(match), []).append(match)
    return groups


def _first_visible(
    candidates: Iterable[tuple[uuid.UUID | None, ResolutionLevel]],
    visible_ids: set[uuid.UUID],
) -> ResolutionResult:
    """Walk `candidates` in order, returning the first id present in `visible_ids`.

    A candidate id that is `None` (unset) or absent from `visible_ids`
    (dangling/cross-workspace, FR-017) is skipped -- the walk falls
    through to the next candidate. `NONE_RESOLVED` if none qualify.
    """
    for candidate_id, level in candidates:
        if candidate_id is not None and candidate_id in visible_ids:
            return ResolvedProfile(profile_id=candidate_id, level=level)
    return NONE_RESOLVED


def resolve_group(
    *,
    competitor_default_id: uuid.UUID | None,
    workspace_default_id: uuid.UUID | None,
    global_default_id: uuid.UUID | None,
    visible_ids: set[uuid.UUID],
    domain_strategy_id: uuid.UUID | None = None,
) -> ResolutionResult:
    """Resolve the group-level (per `(competitor_id, url_pattern)`) profile.

    Walks, in order: domain-strategy (`domain_strategy_id`, always `None`
    in this spec -- SPEC-12's backing table doesn't exist yet, so this
    step is a tolerated no-op that is simply skipped, never an error,
    FR-015) -> competitor default -> workspace default -> global default
    (FR-014). Each candidate counts only if it is a member of
    `visible_ids` (own+global); otherwise it is treated as unset and the
    walk falls through to the next level (FR-017). Returns
    :data:`NONE_RESOLVED` when nothing in the chain -- including the
    global default -- supplies a visible id (FR-016).

    Match-level overrides are **not** applied here -- see
    :func:`apply_match_override`, called per match after this (cached)
    group result.
    """
    candidates: list[tuple[uuid.UUID | None, ResolutionLevel]] = [
        (domain_strategy_id, "domain_strategy"),
        (competitor_default_id, "competitor"),
        (workspace_default_id, "workspace"),
        (global_default_id, "global"),
    ]
    return _first_visible(candidates, visible_ids)


def apply_match_override(
    group_result: ResolutionResult,
    override_id: uuid.UUID | None,
    visible_ids: set[uuid.UUID],
) -> ResolutionResult:
    """Apply the per-match override on top of a (possibly cached) group result.

    The match-level override is the highest-precedence step of the chain
    (FR-014 scenario 1) -- applied per match, after the group's
    resolution (which may be Redis-cached and shared across every match
    in the group). If `override_id` is set and visible (own+global) it
    wins outright; a dangling/cross-workspace override is treated as
    unset (FR-017) and `group_result` is returned unchanged.
    """
    if override_id is not None and override_id in visible_ids:
        return ResolvedProfile(profile_id=override_id, level="match")
    return group_result


def resolution_cache_key(
    workspace_id: uuid.UUID | str,
    competitor_id: uuid.UUID | str,
    url_pattern: str,
) -> str:
    """Deterministic, collision-free, bounded-length Redis key (FR-019).

    `f"profres:{workspace_id}:{competitor_id}:{sha1(url_pattern)}"` --
    `url_pattern` is hashed (rather than concatenated raw) so the key is
    bounded length regardless of pattern size and so that distinct
    `url_pattern` values sharing a prefix/substring can never collide via
    naive string concatenation.
    """
    digest = hashlib.sha1(url_pattern.encode("utf-8")).hexdigest()
    return f"profres:{workspace_id}:{competitor_id}:{digest}"


# --- Redis resolution-cache VALUE codec (SPEC-07 tasks.md T055) --------------
#
# Shared by `apps/api/app/services/profile_resolution.py` (the orchestrator
# that populates the cache) and
# `apps/scrapers/price_monitor/spiders/generic_price_spider.py` (which reads
# the same warm cache, `apps -> libs` only per plan.md) -- both previously
# carried byte-identical copies of this codec. Hoisted here so the two call
# sites can never silently drift apart; the wire format is unchanged, so
# existing cache entries still decode.

#: The cached-value marker for a group that resolved to NONE_RESOLVED --
#: distinct from any real profile id string.
CACHE_NONE_MARKER = "none"
CACHE_FIELD_SEP = "|"


def encode_group_result(result: ResolutionResult) -> str:
    """Encode a (possibly cached) group-level resolution result for Redis."""
    if result is NONE_RESOLVED:
        return CACHE_NONE_MARKER
    assert isinstance(result, ResolvedProfile)
    return f"{result.profile_id}{CACHE_FIELD_SEP}{result.level}"


def decode_group_result(cached: str) -> ResolutionResult:
    """Inverse of :func:`encode_group_result`.

    Raises ``ValueError``/``AttributeError`` on a corrupt/unexpected
    payload -- callers treat that as a cache miss (fail-open).
    """
    if cached == CACHE_NONE_MARKER:
        return NONE_RESOLVED
    profile_id_str, _, level = cached.partition(CACHE_FIELD_SEP)
    return ResolvedProfile(profile_id=uuid.UUID(profile_id_str), level=level)  # type: ignore[arg-type]
