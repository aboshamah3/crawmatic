# Contract: Learned-strategy consumption at dispatch (FR-013..FR-015, US2)

**Pure resolver**: `app_shared/strategy/resolution.py::resolve_strategy_start(profile, *,
algorithm_version) -> StrategyStart | None`. Framework-agnostic, deterministic, exhaustively unit-tested.
`StrategyStart = (access_method: AccessMethod, extraction_method: ExtractionMethod | None)`.

## Rules

Return the learned start **iff all** hold; otherwise return `None`:
- `profile is not None` for the derived (or override) `(workspace, competitor, domain, url_pattern)` key;
- `profile.status == ACTIVE` **or** (`profile.status == LEARNING` **and** a preferred method is set)
  (FR-013, US2 AS1);
- `profile.status != DISABLED` (FR-014, US2 AS3) and not `DEGRADED`-without-preference;
- `profile.url_pattern_version == algorithm_version` — a mismatched-version pattern is **never** used
  (FR-005/FR-015, US2 AS4).

`None` ⇒ caller uses the **default escalation ladder**: SPEC-10 `access.engine.next_attempt` for access,
the SPEC-06/07 extraction pipeline order for extraction (US2 AS2 — no profile ⇒ default ladder; and a
new `DISCOVERY_REQUIRED` profile is available to be created for the key, D5).

## Lookup + spider integration (D5/D6)

In `generic_price_spider.load_targets` (off-reactor, per `(competitor_id, url_pattern)` group):
1. Derive the lookup pattern: `domain_strategy_profiles.url_pattern` **override** if present, else
   `derive_url_pattern(url)` at `URL_PATTERN_ALGORITHM_VERSION`; honor
   `domain_access_rules.url_pattern_override` (FR-006, Edge Cases "Manual override").
2. `resolve_or_create_strategy_profile(...)` (workspace-scoped `scoped_select`) → profile row (creating a
   `DISCOVERY_REQUIRED` one + enqueuing discovery if absent — D5).
3. `resolve_strategy_start(profile, algorithm_version=URL_PATTERN_ALGORITHM_VERSION)`:
   - non-`None` → seed the group's first `AttemptPlan` access method to `preferred_access_method`, and
     reorder the extraction pipeline to try `preferred_extraction_method` **first**, falling back to the
     full order only if it fails (§16 "for learned domains, start from preferred … and fallback only if
     needed"). SC-001: 100% of subsequent scrapes for a confirmed key start from the learned methods.
   - `None` → unchanged default ladder.
4. Thread `profile_id` + resolved start onto each `TargetBundle`/`ScrapeResult` so stats recording
   (`contracts/stats-buffer.md`) has the profile id without a second query.

## Isolation & correctness

- The profile lookup uses the workspace-scoped repository (`scoped_select(DomainStrategyProfile, ws)`) —
  no-context query returns 0 rows (FR-015, Principle II).
- Version-guarding at the resolver (not the DB query) means a row from an old algorithm version is loaded
  but rejected for use, so a version bump can never silently mix patterns (FR-005).
- Pure resolver = no Scrapy/Twisted import; the spider does only the wiring (Constitution I/V).
