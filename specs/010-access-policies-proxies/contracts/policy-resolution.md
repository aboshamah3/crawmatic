# Contract: Effective-policy resolution (`app_shared.access.resolution` + orchestrator)

Pure precedence chain + Redis-cache codec (SQLAlchemy/Redis/FastAPI-free), mirroring
`app_shared/profiles/resolution.py`. Drives US2's "domain rule overrides workspace default"
(FR-007) and Principle IV's batch-resolve-and-cache mandate.

## Pure core (`app_shared/access/resolution.py`)

```python
@dataclass(frozen=True)
class ResolvedPolicy:
    policy_id: uuid.UUID
    level: Literal["domain_rule", "workspace", "global"]

NONE_RESOLVED = _NoneResolved()          # explicit sentinel, falsy, compare with `is`

def select_domain_rule(rules, *, domain, url) -> object | None:
    """Most-specific enabled match: among `rules` with `enabled` and matching `domain`,
    a rule whose `url_pattern` matches `url` beats a domain-only (`url_pattern is None`)
    rule; disabled rules are ignored (Edge Cases). Deterministic tie-break by pattern
    length then id. Pure — `rules` are any objects exposing .enabled/.domain/.url_pattern/
    .access_policy_id/.id."""

def resolve_effective_policy(*, domain_rule_policy_id, workspace_default_policy_id,
                             global_default_policy_id, visible_ids) -> ResolvedPolicy | _NoneResolved:
    """Precedence: domain_rule -> workspace default -> global default (FR-007). A candidate
    id counts only if in `visible_ids` (own+global); otherwise falls through
    (dangling/cross-workspace tolerated). NONE_RESOLVED if nothing qualifies."""

def access_resolution_cache_key(workspace_id, competitor_id, domain, url_pattern) -> str
    # f"accres:{ws}:{competitor}:{sha1(domain|url_pattern)}" — bounded, collision-free.
def encode_result(r) -> str  /  def decode_result(s) -> ResolvedPolicy | _NoneResolved
```

## Orchestrator (`apps/api/app/services/access_resolution.py`)

Mirrors `services/profile_resolution.py`: given a batch of matches, group by
`(competitor_id, domain, url_pattern)`, check the Redis cache per group, on miss do the
**bounded** loads (workspace default policy, global default policy, the competitor's enabled
domain rules via `scoped_select`, the visible-policy id set via `visible_policies_select`),
run the pure core once per group, cache the result (TTL = `Settings.ACCESS_RESOLUTION_CACHE_TTL_SECONDS`,
default 30), and return one `ResolvedPolicy | NONE_RESOLVED` per match. The spider reads the
**same** cache key (duplicated bounded-load shape, `apps -> libs` only — the SPEC-07 precedent).

Workspace/global "default policy" pointer: resolved by the reserved global name
`global_default` (like `GLOBAL_DEFAULT_PROFILE_NAME`) and/or a `workspaces.default_access_policy_id`
column if one is later added; for this spec the workspace default is the workspace's policy
named per convention, and the global default is the `workspace_id IS NULL` policy named
`global_default`. (No new `workspaces` column required — resolution reads by visible policies.)

## Acceptance

- Precedence table (unit): enabled domain rule wins over workspace default wins over global
  (SC-004, 100%). Disabled domain rule → falls through to default. URL-pattern rule beats
  domain-only rule for a matching URL; domain-only rule applies when no pattern matches.
- Dangling/cross-workspace candidate id → skipped, not an error.
- Batch resolution walks each group once (assert one cache write per distinct group, N matches
  in a group → 1 chain walk) — Principle IV / SC (no per-match query storm).
- Cache round-trip: `decode_result(encode_result(r)) == r` incl. `NONE_RESOLVED`.
