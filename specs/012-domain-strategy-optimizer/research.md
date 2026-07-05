# Research: Domain Strategy Optimizer (SPEC-12)

**Feature dir**: `specs/012-domain-strategy-optimizer` | **Date**: 2026-07-05

Phase 0 output. Every decision below resolves a design unknown into a concrete,
doc-grounded choice that reuses existing SPEC-01..11 infrastructure. There are **no**
open `NEEDS CLARIFICATION` items — the spec's Clarifications section already resolved the
three product questions doc-first, and the remaining unknowns are purely technical and
answered here against the master doc (`/srv/crawmatic/PROJECT_SPEC.md` §14/§15/§22/§26/§29/§32)
and the established repo conventions.

Format per decision: **Decision** / **Rationale** / **Alternatives considered**.

---

## D1 — Reuse the existing `AccessMethod` / `ExtractionMethod` enums as `method_name` values

**Decision**: `strategy_attempt_stats.method_name` (and the profile's `preferred_access_method` /
`preferred_extraction_method`) store values from the **already-shipped** `app_shared.enums.AccessMethod`
(`DIRECT_HTTP`, `DIRECT_HTTP_RETRY`, `PROXY_HTTP`, `PLAYWRIGHT_PROXY`) and `app_shared.enums.ExtractionMethod`
(`JSON_LD`, `CSS`, `REGEX`, `SINGLE_NUMBER`, `PLATFORM_JSON`, `EMBEDDED_JSON`, `XPATH`, `PLAYWRIGHT`).
These are validated at the application layer, not in the DB (`method_name` is a plain `String`/`Text`
column, not `enum_column`, because it holds two different enum vocabularies distinguished by `method_type`).

**Rationale**: The per-attempt learning signal is sourced from the existing SPEC-07 spider
`request_attempts` path (spec Clarification #1), and those rows already record `AccessMethod` /
`ExtractionMethod` values via `enum_column`. Learning must key on *exactly the strings the attempt
signal emits*, so reusing the shipped enums guarantees the recorded stat key matches the observed
attempt with zero translation. §14/FR-008's method lists (`PLATFORM_PATTERN`, `CSS_SELECTOR`,
`PLAYWRIGHT_RENDERED_SELECTOR`, ...) are the *doc's descriptive spelling*; the code enum is the
operative vocabulary (`PLATFORM_JSON`≈platform pattern, `CSS`≈CSS selector, `PLAYWRIGHT`≈playwright
rendered selector) and is the source of truth because it is what `price_observations.extraction_method`
and the extraction pipeline already emit. No widening migration and no new extraction-method column.

**Alternatives considered**: (a) Introduce fresh `PLATFORM_PATTERN`/`CSS_SELECTOR`/... members —
rejected: creates two spellings for one concept and a translation layer between the attempt signal
and the stat key, the exact drift the shipped forward-compat enum was designed to avoid. (b) Store
`method_name` via `enum_column(AccessMethod)` — rejected: one column must hold *both* access and
extraction vocabularies (disambiguated by `method_type`), so a single-enum `TypeDecorator` cannot
validate it; validation is done in the strategy service against the correct enum per `method_type`.

## D2 — New status/type enums (VARCHAR, app-validated, no DB enum)

**Decision**: Add to `app_shared.enums` three new `StrEnum`s rendered as `enum_column` VARCHARs:
- `StrategyStatus`: `DISCOVERY_REQUIRED`, `LEARNING`, `ACTIVE`, `DEGRADED`, `DISABLED` (§22).
- `MethodType`: `ACCESS`, `EXTRACTION` (§22).
- `DiscoveryRunStatus`: `PENDING`, `RUNNING`, `COMPLETED`, `NO_WINNER`, `FAILED` (spec US3 AS1/AS4 —
  `COMPLETED` with a winner, `NO_WINNER` when discovery finds nothing, `FAILED` on error).

**Rationale**: Mirrors every prior spec (SPEC-05..11): enum-like columns are plain app-validated
VARCHAR via `enum_column`, never a Postgres-native `ENUM` and never `sqlalchemy.Enum`, so adding a
member later never needs a widening migration ([analyze A2] precedent). `NO_WINNER` distinct from
`FAILED` lets US3 AS4 ("discovery finds no working combination") be recorded as a *successful run
with no winner* separate from an *errored run*.

**Alternatives considered**: Reuse `ScrapeJobStatus` for discovery runs — rejected: discovery-run
lifecycle (`NO_WINNER`) differs from a scrape job's, and conflating them couples two unrelated tables.

## D3 — Three non-partitioned, workspace-owned tables; transitive RLS for `strategy_attempt_stats`

**Decision**: Create exactly the three §22 tables with the §22 columns verbatim:
`domain_strategy_profiles`, `strategy_attempt_stats`, `strategy_discovery_runs`. **Not partitioned**
(they are the learned/rolled-up layer — spec Assumptions, §29 partitions only the append-heavy
`price_observations`/`request_attempts`/`webhook_events`/`price_alert_events`). All identifiers are
UUIDv7 via the shared `Base` (FR-028).
- `domain_strategy_profiles` and `strategy_discovery_runs` carry `workspace_id` directly
  (`WorkspaceScopedBase`, real FK to `workspaces.id`, standard `emit_rls_policy` in the creating
  migration). `domain_strategy_profiles` composite-keys `(workspace_id, competitor_id)` against
  `competitors(workspace_id, id)` (workspace-local FK, structurally cross-workspace-proof, the
  SPEC-05 precedent).
- `strategy_attempt_stats` has **no** `workspace_id` column (§22 does not list one). Its isolation is
  **transitive via its `domain_strategy_profile_id` FK**. A new RLS emitter,
  `emit_fk_transitive_rls_policy(table, parent_table, fk_column, parent_pk="id")`, renders a policy
  `USING (EXISTS (SELECT 1 FROM {parent} p WHERE p.id = {table}.{fk} AND p.workspace_id =
  NULLIF(current_setting('app.workspace_id', true), '')::uuid))`. With no workspace context set the
  inner predicate is never true → **zero rows** (fail-closed, same `NULLIF` guard as `emit_rls_policy`).

**Rationale**: FR-026 explicitly says `strategy_attempt_stats` is isolated "transitively … via its
profile"; the §22 model deliberately omits `workspace_id` on that table. An EXISTS-subquery policy on
the FK's parent is the faithful implementation and keeps the table's columns exactly as §22 specifies
(the task's hard constraint). The parent lookup is by `domain_strategy_profiles(id)` PK — indexed,
O(1). Because it has no `workspace_id` attribute, `strategy_attempt_stats` is **excluded** from
`app_shared.repository.WORKSPACE_OWNED_MODELS` (which requires a `workspace_id` column for
`scoped_select`) and is always queried joined to its scoped parent profile — the same "excluded,
query via a dedicated repository" precedent SPEC-10 set for the dual-scope `AccessPolicy`/`ProxyProvider`.

**Alternatives considered**: (a) Denormalize `workspace_id` onto `strategy_attempt_stats` for a
standard `emit_rls_policy` — rejected: contradicts the §22 column list (the task forbids inventing
columns) and duplicates the source of truth. (b) Partition the three tables — rejected: they are
low-write rolled-up state, not append-heavy audit; partitioning them adds cost with no scale benefit
(spec Assumptions).

## D4 — Redis-buffered atomic stats, flushed off-reactor with one `count = count + delta` per key

**Decision**: Per-method attempt counters are buffered in Redis, **keyed by profile identity** exactly
as §14/FR-022 mandate — `stratstat:{profile_id}:{method_type}:{method_name}` (a Redis HASH with fields
`attempt`, `success`, `failure`, and running sums `rt_ms_sum`, `conf_sum` for the averages) plus a
companion SET `straturl:{profile_id}:{method_type}:{method_name}` holding the *distinct qualifying-success
URL fingerprints* (for the "≥3 different URLs" promotion rule). Recording an attempt is `HINCRBY`
(+ `SADD` on a qualifying success) — atomic, O(1), no read-modify-write in Python (§14). This lives in
`app_shared/strategy/stats_buffer.py` (redis-client parameter, stdlib otherwise — the exact
`app_shared/access/budget.py` shape: no Scrapy/Twisted/FastAPI import). **Recording happens inside the
existing SPEC-07 batched-persistence flush** (`scrape_core/pipelines._flush_batch`), which already runs
**off-reactor** via `run_in_thread`, so no new reactor hop and no blocking Redis on the reactor thread
(FR-025, Constitution V). Flush-to-Postgres is a **Celery task** (`maintenance` / periodic) and runs at
**job finalization**: it atomically drains each key (Lua `HGETALL`+`DEL`, returning the delta and
zeroing in one round-trip) and applies a single `UPDATE strategy_attempt_stats SET attempt_count =
attempt_count + :d, …` per key (FR-023 — no app-side read-modify-write). Promotion/rediscovery read
**persisted DB counts + current pending Redis deltas** (non-destructive `HGETALL`, FR-024).

**Rationale**: Directly implements §14 "Atomic stats" and Constitution VIII "no hot-row contention":
thousands of attempts on one domain in one job never issue N stats-row writes — they `HINCRBY` a Redis
key, and at most one `UPDATE` per (profile, method) key lands per flush interval (SC-003). Keying by
`profile_id` honors FR-022's literal key; the profile id is resolved once per `(competitor_id,
url_pattern)` group in `load_targets` (D5) and threaded onto each result, so the hot path never does a
per-attempt profile lookup. The atomic drain-then-add pattern is the reactor-safe, exactly-once-ish
counter flush that mirrors SPEC-10's `budget.py` `INCR` counters and SPEC-11's Lua-on-Redis-server-clock
discipline.

**Alternatives considered**: (a) Per-attempt SQL `UPDATE … SET count = count + 1` — rejected by §14
explicitly (hot row on a single-domain batch). (b) Key the buffer by the natural tuple `(workspace,
competitor, domain, url_pattern)` and resolve the profile only at flush — rejected: violates FR-022's
"keyed by `domain_strategy_profile_id`" and would require a natural-key→profile join on every flush;
resolving the profile once per group (already the group-resolution seam) is cheaper and literal.
(c) `deferToThread` a fresh Redis call per item on the reactor — rejected: the flush is already
off-reactor and batched; recording inside it adds zero reactor hops.

## D5 — Profile get-or-create + discovery auto-enqueue at group-resolution time (`load_targets`)

**Decision**: `load_targets` already resolves the scrape profile and access policy **once per
`(competitor_id, url_pattern)` group** (off-reactor, inside `run_in_thread`). Extend that same seam to
`resolve_or_create_strategy_profile(session, workspace_id, competitor_id, domain, url_pattern)`: look up
the `domain_strategy_profiles` row by the unique key `(workspace_id, competitor_id, domain,
url_pattern)`; if absent, insert one in `DISCOVERY_REQUIRED` (with the current
`URL_PATTERN_ALGORITHM_VERSION`) and enqueue a discovery run via `app_shared.messaging.enqueue(
STRATEGY_DISCOVERY_RUN, queue="strategy_discovery", …)`. The resolved `profile_id`, `status`, and
preferred methods are threaded onto each `TargetBundle`/`ScrapeResult` so both stats recording (D4) and
the learned-start override (D6) have them without a second query.

**Rationale**: Implements FR-016's automatic path ("a new key with no profile is created
`DISCOVERY_REQUIRED` and enqueues a discovery run") at the one place that already batch-resolves per
group (Principle IV — never a per-match walk). Reuses the existing enqueue-by-name producer seam so the
spider never imports `apps/workers` (Constitution I). Both discovery triggers (auto here, operator via
the API in D7) converge on the same `STRATEGY_DISCOVERY_RUN` task (spec Clarification #3).

**Alternatives considered**: Resolve/seed the profile in a Celery task after the job — rejected: the
learned-start override (D6) needs the profile *before* dispatch, so it must be resolved in the same
pre-fetch group pass.

## D6 — Learned-start consumption: pure resolver overriding the access/extraction start

**Decision**: A pure function `app_shared/strategy/resolution.py::resolve_strategy_start(profile, *,
algorithm_version) -> StrategyStart | None` returns the preferred `(access_method, extraction_method)`
when the profile is `ACTIVE` **or** `LEARNING` with a preferred method set, the profile is **not**
`DISABLED`/`DEGRADED`-without-preference, and `profile.url_pattern_version == algorithm_version`
(FR-013/014/015, US2 AS1–AS4). Otherwise it returns `None` and the caller falls back to the **default
escalation ladder** (SPEC-10 `access.engine.next_attempt` for access; the SPEC-06/07 extraction pipeline
order for extraction). The spider consumes this in `load_targets` (D5): a non-`None` result seeds the
first `AttemptPlan` access method and reorders the extraction pipeline to try the preferred method first,
falling back to the full order only if it fails (§16 "for learned domains, start from preferred … and
fallback only if needed").

**Rationale**: Keeps consumption a **pure, version-guarded, workspace-scoped** decision in `app_shared`
(scraping-free, unit-testable exhaustively), with the spider doing only the wiring — the same
pure-logic/thin-seam split SPEC-10 (`access.engine`) and SPEC-11 (`limiter`) used. Version-guarding at
the resolver is what enforces FR-005/FR-015 ("never mix patterns from different algorithm versions").

**Alternatives considered**: Bake the lookup into the access engine — rejected: the access engine is
SPEC-10-owned and access-only; extraction ordering also needs the learned preference, so a dedicated
strategy resolver that feeds both is cleaner and keeps SPEC-10 untouched.

## D7 — Discovery orchestration + operator API on the `strategy_discovery` queue

**Decision**: Discovery is a Celery task `STRATEGY_DISCOVERY_RUN = "strategy_discovery.run_discovery"`
on the existing `strategy_discovery` queue (§26). It: validates `3 ≤ sample_size ≤ 10` (FR-019, reject
otherwise → run recorded `FAILED`/validation error), creates a `strategy_discovery_runs` row
(`PENDING`→`RUNNING`), drives the 3–10 sample matched URLs through the **existing** fetch/extract
pipeline **testing candidate access methods then extraction methods** (the one path allowed to probe
multiple methods — §14, spec Clarification #1), selects the winning `(access, extraction)` combination
by the promotion-quality bar (valid numeric price + valid currency-when-required + confidence ≥
threshold across the sample), writes `winning_access_method`/`winning_extraction_method`/`completed_at`
(or `NO_WINNER`), and **seeds/updates the profile** out of `DISCOVERY_REQUIRED` (`→ LEARNING`, or
`→ ACTIVE` if the sample already satisfies the 3-confirmation rule) via the shared seed helper (D9).
Operator trigger: `POST /v1/strategy/discovery-runs` (workspace-scoped) enqueues the same task;
read endpoints `GET /v1/strategy/profiles[/{id}]` and `GET /v1/strategy/discovery-runs[/{id}]`
(cursor-paginated lists, default 50 / max 500 per §24) let operators inspect learned strategies. Both
trigger paths converge on the one task + seed code path (spec Clarification #3, FR-016).

**Rationale**: §26 already reserves the `strategy_discovery` queue for "domain discovery
orchestration"; a Celery task is fully off-reactor and can legitimately walk multiple methods on a small
sample. Reusing the existing dispatch/spider avoids rebuilding fetch/extract (spec Assumptions). The
thin API mirrors the versioned, cursor-paginated `/v1` surface every prior spec exposes.

**Alternatives considered**: Run discovery inside the spider — rejected: Constitution V (spiders persist
only; multi-method probing is orchestration, not a spider concern). A synchronous API-side discovery —
rejected: discovery does live fetches; it must be async on its queue.

## D8 — Rediscovery: pure evaluator + inline (post-flush) and periodic light re-check triggers

**Decision**: A pure function `app_shared/strategy/rediscovery.py::evaluate_rediscovery(profile,
combined_stats, thresholds) -> RediscoveryDecision` returns *trigger / no-trigger* + reason for the
FR-020 conditions: `recent_failure_count ≥ 3` (consecutive preferred-method failures), per-method
cumulative `success_rate < 0.80` (persisted + pending deltas), repeated empty selector, repeated
confidence `< 0.75`, repeated 403/429 (`ScrapeErrorCode.HTTP_403/HTTP_429`), currency-disappeared,
unrealistic price, template-change signal. On trigger, the caller sets the profile `DEGRADED` and
enqueues `STRATEGY_DISCOVERY_RUN`. `recent_failure_count` is maintained on the profile at flush time
(incremented on a preferred-method failure, reset to 0 on a qualifying success — spec Clarification #2).
Two call sites: **(a)** inline in the stats-flush Celery task (evaluate each just-flushed profile), and
**(b)** a periodic **light re-check** Celery task `STRATEGY_LIGHT_RECHECK =
"maintenance.strategy_light_recheck"` (§28 scheduler-adjacent) that scans `ACTIVE` profiles and enqueues
rediscovery without a full failed batch (FR-021, US4 AS4).

**Rationale**: Pure evaluator = deterministic, unit-testable boundary values (Constitution testing
gate). Driving it from the flush task (which already reads combined counts) and a periodic re-check
covers both "degradation observed during scraping" and "degradation caught by patrol" (SC-004: marked
degraded + rediscovery enqueued within one evaluation cycle).

**Alternatives considered**: Evaluate rediscovery on the reactor as attempts stream in — rejected:
Constitution V (no such logic on the reactor); the flush task already has the combined counts off-reactor.

## D9 — Promotion: pure evaluator applied at flush time with a guarded atomic update

**Decision**: A pure function `app_shared/strategy/promotion.py::evaluate_promotion(combined_stats,
distinct_url_count, thresholds) -> PromotionDecision` returns whether a method qualifies: `success_count
≥ 3` **AND** `distinct_url_count ≥ 3` (the SET's `SCARD`) — each success already gated at record time on
confidence ≥ threshold (default `0.85`), valid numeric price, and valid currency-when-required (so
non-qualifying attempts never entered the count/SET — FR-010, US1 AS2/AS3). Applied in the flush Celery
task: on a qualifying access method, set `preferred_access_method` + `access_confidence`; on a
qualifying extraction method, set `preferred_extraction_method` + `extraction_confidence`; bump
`confirmed_success_count`; move the profile to `ACTIVE` (FR-011). Concurrency: the write is a **guarded
atomic `UPDATE … WHERE id = :id AND status IN ('DISCOVERY_REQUIRED','LEARNING','DEGRADED')`** so two
workers cannot double-promote; the unique `(profile_id, method_type, method_name)` on stats and the
single-`UPDATE`-per-key flush protect the counts (Edge Cases "Concurrent promotion").

**Rationale**: Implements FR-010/FR-011 and US1 exactly, with the distinct-URL requirement enforced by
the Redis SET populated only by *qualifying* successes (so US1 AS2 — "3 successes but 2 URLs" — cannot
promote). Gating each success at record time keeps the qualification predicate (confidence/price/currency)
next to the money/confidence validation SPEC-06/07 already own (Constitution VII). The guarded UPDATE is
the same optimistic-concurrency shape used across the repo for idempotent state transitions.

**Alternatives considered**: Promote inside `load_targets` when reading the profile — rejected: promotion
needs the combined counts and the distinct-URL SET, which are a flush-time concern, not a pre-fetch one.

## D10 — Reuse the shipped `derive_url_pattern` + `URL_PATTERN_ALGORITHM_VERSION`; backfill is a maintenance task

**Decision**: Reuse `app_shared.url_pattern.derive_url_pattern` and `URL_PATTERN_ALGORITHM_VERSION`
(currently `1`) unchanged — they already implement the full §15 algorithm (lowercase host, strip
`www.`/scheme/query/fragment/trailing-slash, preserve locale prefix, `:id` for id-like segments, `*`
for product-slug segments after `products`/`product`/`p`/`item`) and are already the source of
`competitor_product_matches.url_pattern`/`url_pattern_version`. `domain_strategy_profiles.url_pattern_version`
is stamped from the same constant. A manual `domain_strategy_profiles.url_pattern` override (and the
SPEC-10 `domain_access_rules.url_pattern_override`) takes precedence over the derived value (FR-006,
Edge Cases "Manual override"). FR-005's version-bump backfill is a documented **maintenance** Celery
task (`STRATEGY_PATTERN_BACKFILL = "maintenance.strategy_pattern_backfill"`) that re-derives + re-links
(or re-queues discovery for) profiles when the constant is bumped; at version 1 it is a defined
mechanism, not yet exercised.

**Rationale**: The whole point of FR-001..FR-004 already shipped in SPEC-05; SPEC-12 must *reuse* it so
matches and strategies share one join key at one version (spec Assumptions, §15). No new derivation code.

**Alternatives considered**: Re-implement derivation locally — rejected: duplicates the shipped
algorithm and risks version drift between matches and strategies (the exact failure §15 versioning
guards against).

## D11 — Tunable thresholds live in `Settings`; no hardcoded promotion/rediscovery constants

**Decision**: Add env-tunable knobs to `app_shared.config.Settings` with the documented defaults:
`STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD: float = 0.85`, `STRATEGY_PROMOTION_MIN_SUCCESSES: int = 3`,
`STRATEGY_PROMOTION_MIN_DISTINCT_URLS: int = 3`, `STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR: float = 0.80`,
`STRATEGY_REDISCOVERY_LOW_CONFIDENCE: float = 0.75`, `STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES: int = 3`,
`STRATEGY_DISCOVERY_MIN_SAMPLE: int = 3`, `STRATEGY_DISCOVERY_MAX_SAMPLE: int = 10`,
`STRATEGY_STATS_FLUSH_INTERVAL_SECONDS: int = 60`, `STRATEGY_STATS_KEY_TTL_SECONDS` (slack over a job's
lifetime so a crashed writer's buffer self-evicts).

**Rationale**: Constitution IV (Database/config-driven behavior — thresholds are configurable, not
hardcoded) and spec Assumptions ("configurable with the documented defaults"). Matches SPEC-11's pattern
of parking every numeric knob in `Settings`.

**Alternatives considered**: Per-workspace threshold columns — rejected as out of scope for v1; global
`Settings` defaults suffice and the spec asks only for "configurable with documented defaults."

---

## Reactor-safety proof reuse

The existing static AST grep test (`tests/unit/test_reactor_safety_grep.py`) already scans
`scrape_core/pipelines.py` and the spider for `time.sleep`/synchronous-Redis calls outside a
`run_in_thread`/`deferToThread` boundary. Because SPEC-12's only reactor-adjacent addition is the
stats-buffer `HINCRBY`/`SADD` **inside `_flush_batch`** (already an off-reactor entry point), that test
continues to guarantee FR-025/SC-007 with no structural change — the new calls sit inside an
already-"safe" transitive set. The plan's quickstart adds an explicit assertion that the strategy stats
recorder is only ever called from within that off-reactor flush.

## Infra-absence testing posture

Consistent with SPEC-05..11: pure logic (URL pattern reuse, promotion/rediscovery/consumption
evaluators, Redis buffer key math against a fake/real Redis, RLS-DDL string rendering, migration
single-head) is exhaustively **unit-tested** with no infra. Live behaviors (RLS zero-rows under a real
Postgres, discovery driving a real Scrapyd sample, end-to-end flush→promote) are **integration tests
that skip cleanly** when Postgres/Redis/Scrapyd are absent in this build environment.
