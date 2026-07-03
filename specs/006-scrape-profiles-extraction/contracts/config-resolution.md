# Contract: config-resolution (`app_shared/profiles/resolution.py` core + `apps/api/app/services/profile_resolution.py` orchestrator)

The core deliverable (US3): given a match, return the single scrape profile that applies, by walking the §9 chain — batch-resolved per `(competitor_id, url_pattern)`, Redis-cached.

## Pure core (`app_shared/profiles/resolution.py`, no DB/Redis)

### Sentinel

```python
NONE_RESOLVED  # explicit "no profile resolved" marker (FR-016) — not an error, not a row
```

`ResolvedProfile` = a resolved `profile_id: uuid.UUID` + the `level` that supplied it (`"match" | "competitor" | "workspace" | "global"`), **or** `NONE_RESOLVED`.

### Grouping (FR-018, SC-004)

```python
def group_key(match) -> tuple[uuid.UUID, str]:   # (competitor_id, url_pattern)
def group_matches(matches) -> dict[tuple, list]  # distinct groups -> their matches
```

### Group resolution (steps 2–5, invariant within a group)

```python
def resolve_group(
    *, competitor_default_id, workspace_default_id, global_default_id,
    visible_ids: set[uuid.UUID], domain_strategy_id=None,
) -> ResolvedProfile | NONE_RESOLVED
```

Walk in order — domain-strategy (`domain_strategy_id`, always `None` this spec → skipped, FR-015) → competitor default → workspace default → global default. A candidate id counts only if it is in `visible_ids` (own+global); a dangling/cross-workspace id is skipped (FR-017). No visible id at any step → `NONE_RESOLVED`.

### Match override (step 1, per match)

```python
def apply_match_override(group_result, override_id, visible_ids) -> ResolvedProfile | NONE_RESOLVED
```

If `override_id` is set and visible → return it (level `"match"`, highest precedence, FR-014 scenario 1); else return `group_result`.

### Cache key (FR-019, §9)

```python
def resolution_cache_key(workspace_id, competitor_id, url_pattern) -> str
#   f"profres:{workspace_id}:{competitor_id}:{sha1(url_pattern).hexdigest()}"
```

Deterministic, collision-free per tuple, bounded length (url_pattern hashed).

## Orchestrator (`apps/api/app/services/profile_resolution.py`, DB + Redis)

`resolve_profiles_for_matches(session, redis, workspace_id, matches) -> {match_id: ResolvedProfile|NONE_RESOLVED}`:

1. `group_matches(matches)` → distinct `(competitor_id, url_pattern)` groups.
2. **Bounded loads** (no per-match query): one read of `workspaces.default_scrape_profile_id`; one `scoped`/consistency `IN (...)` read of `competitors.default_scrape_profile_id` over the distinct competitor ids; one `visible_profiles_select(ws)` `IN (...)` read to build `visible_ids` for every candidate id referenced; one lookup of the global default (`visible_profiles_select` filtered to `workspace_id IS NULL AND name == GLOBAL_DEFAULT_PROFILE_NAME`).
3. Per group: Redis `GET resolution_cache_key(...)`; on hit use it; on miss `resolve_group(...)`, then `SET` with TTL `Settings.PROFILE_RESOLUTION_CACHE_TTL_SECONDS` (store the id or `"none"`).
4. Per match: `apply_match_override(group_result, match.scrape_profile_id, visible_ids)`.

`invalidate_resolution_cache(redis, workspace_id, competitor_id=None)`: best-effort `SCAN`+`DEL` over `profres:{ws}:{competitor_id or *}:*`, called after any profile/assignment write; TTL is the backstop (FR-019). Redis errors on read are fail-open (re-walk the chain) — resolution is not a security boundary.

## Rules

- Domain-strategy step is a tolerated no-op until SPEC-12 (FR-015) — skipped cleanly, chain proceeds.
- No per-match DB access at 10k–20k matches (SC-004): a fixed set of `IN (...)` loads + one grouped walk per group + Redis.
- Terminal fallback is the global default; absent → `NONE_RESOLVED` (FR-016).
- Cross-workspace/dangling refs never leak (FR-017): filtered by `visible_ids`.

## Tests

- **Unit (no DB/Redis)**: chain ordering across every precedence combination; visibility fall-through (id not in `visible_ids` → skip); domain-strategy `None` skipped; `NONE_RESOLVED` when nothing visible; `group_matches` yields one result per group applied to all its matches; match override beats group result; `resolution_cache_key` deterministic + collision-free.
- **Live (PG+Redis, marked)**: batch of ≥10k matches over a few groups → lookups proportional to groups, not matches; second resolution within TTL served from cache; a relevant profile/assignment write invalidates or expires the entry.
