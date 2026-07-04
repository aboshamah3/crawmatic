"""Pure effective-policy resolution core (`contracts/policy-resolution.md`, SPEC-10 US2, FR-007).

Mirrors `app_shared/profiles/resolution.py`'s shape. Pure, stdlib only --
no SQLAlchemy/Redis/FastAPI imports (grep-enforced by the caller's
verification step). Given an already-loaded list of `DomainAccessRule`
candidates plus the workspace/global default policy ids, decides which
`AccessPolicy` applies to a `(competitor_id, domain, url_pattern)` group
-- the orchestrator (`apps/api/app/services/access_resolution.py`) and
the spider (`apps/scrapers/price_monitor/spiders/generic_price_spider.py`)
drive this core with the bounded DB loads + the Redis cache; this module
performs **no** I/O.

## Judgment call: what "a rule's `url_pattern` matches `url`" means

`data-model.md` describes `url_pattern` only as "an optional pattern; a
URL-pattern rule beats a domain-only rule (most specific wins)" without
pinning down the matching algorithm. This module treats a non-`None`
`url_pattern` as a **substring** that must appear in `url` --
deterministic, dependency-free, and simple for an operator to reason
about when authoring a rule (e.g. a `url_pattern` of `"/electronics/"`
matches any URL whose path contains that segment). Among multiple
substring matches, the **longest** `url_pattern` wins (most specific),
tie-broken by `id` for full determinism.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

ResolutionLevel = Literal["domain_rule", "workspace", "global"]


@dataclass(frozen=True)
class ResolvedPolicy:
    """A resolved access-policy id plus the precedence level that supplied it."""

    policy_id: uuid.UUID
    level: ResolutionLevel


class _NoneResolved:
    """Singleton sentinel: no level in the chain supplied a visible policy.

    A distinct, explicit result type -- not an error, not an arbitrary
    row, and not confusable with a real :class:`ResolvedPolicy`.
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


#: Explicit "no policy resolved" marker. Compare with `is`.
NONE_RESOLVED = _NoneResolved()

#: The shape returned by :func:`resolve_effective_policy`.
ResolutionResult = ResolvedPolicy | _NoneResolved


def select_domain_rule(rules: Iterable[object], *, domain: str, url: str) -> object | None:
    """Most-specific enabled `DomainAccessRule`-shaped match, or `None`.

    `rules` are any objects exposing `.enabled`/`.domain`/`.url_pattern`/
    `.access_policy_id`/`.id` (e.g. ORM `DomainAccessRule` rows, or a
    plain dataclass/namedtuple in tests). Disabled rules are ignored.
    Among rules matching `domain`, a rule whose `url_pattern` is a
    substring of `url` beats a domain-only rule (`url_pattern is None`);
    tie-broken by pattern length (longest/most-specific wins) then `id`.
    """
    enabled_for_domain = [rule for rule in rules if rule.enabled and rule.domain == domain]

    pattern_candidates = [
        rule
        for rule in enabled_for_domain
        if rule.url_pattern is not None and rule.url_pattern in url
    ]
    if pattern_candidates:
        pattern_candidates.sort(key=lambda rule: (-len(rule.url_pattern), str(rule.id)))
        return pattern_candidates[0]

    domain_only_candidates = [rule for rule in enabled_for_domain if rule.url_pattern is None]
    if not domain_only_candidates:
        return None
    domain_only_candidates.sort(key=lambda rule: str(rule.id))
    return domain_only_candidates[0]


def resolve_effective_policy(
    *,
    domain_rule_policy_id: uuid.UUID | None,
    workspace_default_policy_id: uuid.UUID | None,
    global_default_policy_id: uuid.UUID | None,
    visible_ids: set[uuid.UUID],
) -> ResolutionResult:
    """Precedence: domain rule -> workspace default -> global default (FR-007).

    A candidate id counts only if it is a member of the caller-supplied
    `visible_ids` (own+global, from
    `app_shared.access.repository.visible_policies_select`); a
    dangling/cross-workspace id is treated as unset and the chain falls
    through to the next level. `NONE_RESOLVED` if nothing qualifies --
    never an error, never an arbitrary row.
    """
    candidates: list[tuple[uuid.UUID | None, ResolutionLevel]] = [
        (domain_rule_policy_id, "domain_rule"),
        (workspace_default_policy_id, "workspace"),
        (global_default_policy_id, "global"),
    ]
    for candidate_id, level in candidates:
        if candidate_id is not None and candidate_id in visible_ids:
            return ResolvedPolicy(policy_id=candidate_id, level=level)
    return NONE_RESOLVED


def access_resolution_cache_key(
    workspace_id: uuid.UUID | str,
    competitor_id: uuid.UUID | str,
    domain: str,
    url_pattern: str | None,
) -> str:
    """Deterministic, bounded-length Redis key.

    `f"accres:{workspace_id}:{competitor_id}:{sha1(domain|url_pattern)}"`
    -- `domain`/`url_pattern` are hashed together (rather than
    concatenated raw into the key) so the key is bounded length
    regardless of input size and collision-free across distinct
    `(domain, url_pattern)` pairs.
    """
    digest = hashlib.sha1(f"{domain}|{url_pattern}".encode("utf-8")).hexdigest()
    return f"accres:{workspace_id}:{competitor_id}:{digest}"


# --- Redis resolution-cache VALUE codec -------------------------------------
#
# Shared by `apps/api/app/services/access_resolution.py` (the orchestrator
# that populates the cache) and
# `apps/scrapers/price_monitor/spiders/generic_price_spider.py` (which reads
# the same warm cache, `apps -> libs` only) -- both read/write via this one
# codec so the two call sites can never silently drift apart.

#: The cached-value marker for a group that resolved to NONE_RESOLVED --
#: distinct from any real policy id string.
CACHE_NONE_MARKER = "none"
CACHE_FIELD_SEP = "|"


def encode_result(result: ResolutionResult) -> str:
    """Encode a (possibly cached) resolution result for Redis."""
    if result is NONE_RESOLVED:
        return CACHE_NONE_MARKER
    assert isinstance(result, ResolvedPolicy)
    return f"{result.policy_id}{CACHE_FIELD_SEP}{result.level}"


def decode_result(cached: str) -> ResolutionResult:
    """Inverse of :func:`encode_result`.

    Raises ``ValueError``/``AttributeError`` on a corrupt/unexpected
    payload -- callers treat that as a cache miss (fail-open).
    """
    if cached == CACHE_NONE_MARKER:
        return NONE_RESOLVED
    policy_id_str, _, level = cached.partition(CACHE_FIELD_SEP)
    return ResolvedPolicy(policy_id=uuid.UUID(policy_id_str), level=level)  # type: ignore[arg-type]
