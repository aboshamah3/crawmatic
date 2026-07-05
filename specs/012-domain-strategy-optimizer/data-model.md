# Data Model: Domain Strategy Optimizer (SPEC-12)

**Feature dir**: `specs/012-domain-strategy-optimizer` | **Date**: 2026-07-05

Phase 1 output. Three new **workspace-owned, non-partitioned** Postgres tables (the learned/rolled-up
layer — §22, spec Assumptions), new app-validated enums, one new RLS emitter, a set of Redis key
families for buffered stats, new `Settings` knobs, new Celery task-name constants, and the reused
`url_pattern`/derivation/access/extraction surfaces. Column/enum names are taken **verbatim from §22**
and the spec's Key Entities. All PKs are UUIDv7 via the shared `Base` (FR-028). See `research.md` for
the decisions (D1–D11) behind each choice and `contracts/` for the behavioral surfaces.

---

## 1. Enums (`app_shared/enums.py`, additions — all VARCHAR via `enum_column`)

| Enum | Members | Source |
|------|---------|--------|
| `StrategyStatus` | `DISCOVERY_REQUIRED`, `LEARNING`, `ACTIVE`, `DEGRADED`, `DISABLED` | §22 "Strategy status" / FR-007 |
| `MethodType` | `ACCESS`, `EXTRACTION` | §22 "Method type" / FR-008 |
| `DiscoveryRunStatus` | `PENDING`, `RUNNING`, `COMPLETED`, `NO_WINNER`, `FAILED` | US3 AS1/AS4 (research D2) |

**Reused, not re-declared** (research D1): `AccessMethod` (`DIRECT_HTTP`, `DIRECT_HTTP_RETRY`,
`PROXY_HTTP`, `PLAYWRIGHT_PROXY`) and `ExtractionMethod` (`JSON_LD`, `CSS`, `REGEX`, `SINGLE_NUMBER`,
`PLATFORM_JSON`, `EMBEDDED_JSON`, `XPATH`, `PLAYWRIGHT`) — these are the `method_name` vocabularies,
disambiguated by `method_type`. `ScrapeErrorCode` (`HTTP_403`/`HTTP_429`/… and `STRATEGY_DEGRADED`,
already forward-declared) supplies rediscovery signal codes.

### State transitions — `domain_strategy_profiles.status`

```
                (new key, no profile)
                         │
                         ▼
                 DISCOVERY_REQUIRED ──discovery seeds a winner──▶ LEARNING ──3-confirmation promotion──▶ ACTIVE
                         │                                          │                                      │
                         └── discovery NO_WINNER ──▶ (stays          └── promotion (already ≥3 confirmed) ─┘
                             DISCOVERY_REQUIRED /                                                          │
                             recorded on the run)                     rediscovery trigger (FR-020) ◀───────┘
                                                                                 │
                                                                                 ▼
                                                                             DEGRADED ──discovery──▶ LEARNING/ACTIVE
   DISABLED: operator-set; learned preference never applied (FR-014); no automatic transition out.
```

### State transitions — `strategy_discovery_runs.status`

```
PENDING ──picked up──▶ RUNNING ──winner found──▶ COMPLETED (winning_* + completed_at set)
                          │
                          ├── no working combo ──▶ NO_WINNER (completed_at set, winning_* NULL)
                          └── error / invalid sample_size ──▶ FAILED
```

---

## 2. Table: `domain_strategy_profiles` (workspace-owned, RLS, not partitioned)

ORM: `app_shared/models/strategy.py::DomainStrategyProfile(Base, WorkspaceScopedBase, TimestampMixin)`.
Columns verbatim from §22:

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7) | no | PK (`Base`) |
| `workspace_id` | `Uuid` | no | `WorkspaceScopedBase`; real FK → `workspaces.id`; RLS anchor |
| `competitor_id` | `Uuid` | no | workspace-local composite FK `(workspace_id, competitor_id)` → `competitors(workspace_id, id)` |
| `domain` | `Text` | no | |
| `url_pattern` | `Text` | no | derived (`derive_url_pattern`) or manual override (FR-006) |
| `url_pattern_version` | `Integer` | no | stamped from `URL_PATTERN_ALGORITHM_VERSION` (FR-004) |
| `status` | `enum_column(StrategyStatus)` | no | default `DISCOVERY_REQUIRED` |
| `preferred_access_method` | `enum_column(AccessMethod)` | **yes** | set on promotion (FR-011) |
| `preferred_extraction_method` | `enum_column(ExtractionMethod)` | **yes** | set on promotion (FR-011) |
| `access_confidence` | `Numeric(5,4)` | yes | in `[0,1]`, never `Money` |
| `extraction_confidence` | `Numeric(5,4)` | yes | in `[0,1]` |
| `confirmed_success_count` | `Integer` | no | default 0 (FR-011) |
| `recent_failure_count` | `Integer` | no | default 0; ++ on preferred-method failure, reset to 0 on qualifying success (FR-012/FR-020, Clarification #2) |
| `last_discovery_at` | `TZDateTime` | yes | |
| `last_success_at` | `TZDateTime` | yes | |
| `last_failed_at` | `TZDateTime` | yes | |
| `created_at` / `updated_at` | `TZDateTime` | no | `TimestampMixin` |

**Constraints / indexes**:
- `UniqueConstraint(workspace_id, competitor_id, domain, url_pattern)` (FR-007/FR-027) — name shortened
  to fit Postgres's 63-byte cap, e.g. `uq_dsp_ws_competitor_domain_pattern` (the `cpm` shorthand
  precedent from `competitor_product_matches`).
- `ForeignKeyConstraint((workspace_id, competitor_id) → competitors(workspace_id, id))` — structural
  cross-workspace-proof (SPEC-05 precedent) — name `fk_dsp_workspace_competitor_competitors`.
- `ForeignKeyConstraint(workspace_id → workspaces.id)` (RLS anchor).
- Indexed `workspace_id` (from `WorkspaceScopedBase`); lookup index on
  `(workspace_id, competitor_id, domain, url_pattern, url_pattern_version)` supports the version-guarded
  consumption lookup (D6).
- Registered in `app_shared.repository.WORKSPACE_OWNED_MODELS`.
- RLS: standard `emit_rls_policy("domain_strategy_profiles")` in the creating migration.

## 3. Table: `strategy_attempt_stats` (RLS **transitive via profile**, not partitioned)

ORM: `app_shared/models/strategy.py::StrategyAttemptStats(Base, TimestampMixin?)` — **no**
`WorkspaceScopedBase` (§22 lists no `workspace_id`; research D3). Columns verbatim from §22:

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7) | no | PK |
| `domain_strategy_profile_id` | `Uuid` | no | FK → `domain_strategy_profiles.id` (RLS + isolation anchor, transitive) |
| `method_type` | `enum_column(MethodType)` | no | `ACCESS` \| `EXTRACTION` |
| `method_name` | `Text` | no | plain string validated app-side against `AccessMethod`/`ExtractionMethod` per `method_type` (D1) |
| `attempt_count` | `Integer` | no | default 0 |
| `success_count` | `Integer` | no | default 0 |
| `failure_count` | `Integer` | no | default 0 |
| `success_rate` | `Numeric(5,4)` | no | default 0; maintained at flush (`success/attempt`) |
| `avg_response_time_ms` | `Integer` | yes | maintained from `rt_ms_sum/attempt` at flush |
| `avg_confidence` | `Numeric(5,4)` | yes | maintained from `conf_sum/success` at flush |
| `last_success_at` | `TZDateTime` | yes | |
| `last_failed_at` | `TZDateTime` | yes | |

**Constraints / indexes**:
- `UniqueConstraint(domain_strategy_profile_id, method_type, method_name)` (FR-009/FR-027, protects
  against double-promote/concurrent-corruption) — name `uq_sas_profile_method_type_name`.
- `ForeignKeyConstraint(domain_strategy_profile_id → domain_strategy_profiles.id)` (real FK; the
  parent's `workspace_id` provides transitive isolation).
- Index on `domain_strategy_profile_id`.
- **Excluded** from `WORKSPACE_OWNED_MODELS` (no `workspace_id` column → `scoped_select` cannot scope
  it); queried only joined to its scoped parent profile via a dedicated
  `app_shared/strategy/repository.py` helper (SPEC-10 dual-scope exclusion precedent).
- RLS: **new** `emit_fk_transitive_rls_policy("strategy_attempt_stats",
  parent_table="domain_strategy_profiles", fk_column="domain_strategy_profile_id")` →
  `USING (EXISTS (SELECT 1 FROM domain_strategy_profiles p WHERE p.id =
  strategy_attempt_stats.domain_strategy_profile_id AND p.workspace_id =
  NULLIF(current_setting('app.workspace_id', true), '')::uuid))`. Fail-closed: no context → 0 rows
  (FR-026, SC-005). Added to `app_shared/models/rls.py` alongside the existing emitters.

## 4. Table: `strategy_discovery_runs` (workspace-owned, RLS, not partitioned)

ORM: `app_shared/models/strategy.py::StrategyDiscoveryRun(Base, WorkspaceScopedBase)`. Columns verbatim
from §22:

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | `Uuid` (UUIDv7) | no | PK |
| `workspace_id` | `Uuid` | no | `WorkspaceScopedBase`; real FK → `workspaces.id` |
| `competitor_id` | `Uuid` | no | workspace-local composite FK → `competitors(workspace_id, id)` |
| `domain` | `Text` | no | |
| `url_pattern` | `Text` | no | |
| `sample_size` | `Integer` | no | validated `3 ≤ n ≤ 10` at trigger (FR-019) |
| `status` | `enum_column(DiscoveryRunStatus)` | no | default `PENDING` |
| `winning_access_method` | `enum_column(AccessMethod)` | yes | set on `COMPLETED` (FR-017) |
| `winning_extraction_method` | `enum_column(ExtractionMethod)` | yes | set on `COMPLETED` |
| `created_at` | `TZDateTime` | no | default now |
| `completed_at` | `TZDateTime` | yes | set on `COMPLETED`/`NO_WINNER` (FR-017) |

**Constraints / indexes**: `ForeignKeyConstraint(workspace_id → workspaces.id)`;
`ForeignKeyConstraint((workspace_id, competitor_id) → competitors(workspace_id, id))`; indexed
`workspace_id`; lookup index `(workspace_id, competitor_id, domain, url_pattern)`. Registered in
`WORKSPACE_OWNED_MODELS`; standard `emit_rls_policy`. (No unique constraint — multiple discovery runs
over time for one key are expected/allowed.)

## 5. Migration (single new Alembic revision)

`alembic/versions/<rev>_domain_strategy_optimizer_tables.py`, `down_revision = "851220acab90"` (current
head, SPEC-10 — SPEC-11 added no migration). Hand-authored (no live Postgres in this env), reproducing
the three ORM shapes exactly, creating the two composite-FK anchors, and emitting RLS **in the same
migration**: `emit_rls_policy` for `domain_strategy_profiles` and `strategy_discovery_runs`;
`emit_fk_transitive_rls_policy` for `strategy_attempt_stats`. `downgrade()` drops the three tables in
reverse order. Must keep `scripts/check_single_head.sh` green (one head).

## 6. Redis key families (buffered stats — `noeviction` instance, research D4)

Keyed by **profile id** to honor FR-022 literally. TTL `STRATEGY_STATS_KEY_TTL_SECONDS` on every key so
a crashed writer's buffer self-evicts.

| Key | Type | Fields / members | Purpose |
|-----|------|------------------|---------|
| `stratstat:{profile_id}:{method_type}:{method_name}` | HASH | `attempt`, `success`, `failure`, `rt_ms_sum`, `conf_sum` | atomic `HINCRBY` per attempt; drained (`HGETALL`+`DEL`, one Lua round-trip) at flush → single `count = count + delta` UPDATE (FR-023) |
| `straturl:{profile_id}:{method_type}:{method_name}` | SET | fingerprints of distinct **qualifying-success** URLs | `SADD` on a qualifying success; `SCARD ≥ 3` = distinct-URL promotion gate (FR-010, US1 AS2) |
| `stratdirty:{workspace_id}` | SET | profile ids with pending deltas | flush task enumerates dirty profiles without scanning all keys |

**Recording** (`app_shared/strategy/stats_buffer.py`, redis-client param, stdlib only) runs inside the
existing off-reactor `_flush_batch` (FR-025). **Promotion/rediscovery reads** = persisted DB row +
non-destructive `HGETALL` of the pending delta (FR-024).

## 7. `Settings` additions (`app_shared/config.py`, research D11)

`STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD: float = 0.85`, `STRATEGY_PROMOTION_MIN_SUCCESSES: int = 3`,
`STRATEGY_PROMOTION_MIN_DISTINCT_URLS: int = 3`, `STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR: float = 0.80`,
`STRATEGY_REDISCOVERY_LOW_CONFIDENCE: float = 0.75`, `STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES: int = 3`,
`STRATEGY_DISCOVERY_MIN_SAMPLE: int = 3`, `STRATEGY_DISCOVERY_MAX_SAMPLE: int = 10`,
`STRATEGY_STATS_FLUSH_INTERVAL_SECONDS: int = 60`, `STRATEGY_STATS_KEY_TTL_SECONDS: int = 3600`.

## 8. Celery task-name constants (`app_shared/task_names.py`, additions)

```python
# --- Domain strategy optimizer (SPEC-12) ---
STRATEGY_DISCOVERY_RUN      = "strategy_discovery.run_discovery"      # strategy_discovery queue (§26)
STRATEGY_STATS_FLUSH        = "maintenance.strategy_stats_flush"      # periodic + job-finalization flush
STRATEGY_LIGHT_RECHECK      = "maintenance.strategy_light_recheck"    # periodic degradation patrol (FR-021)
STRATEGY_PATTERN_BACKFILL   = "maintenance.strategy_pattern_backfill" # version-bump backfill (FR-005)
```

Consumed by new/extended worker tasks; enqueued via the existing `app_shared.messaging.enqueue`
producer seam (no `apps/workers` import from the spider — Constitution I).

## 9. Reused surfaces (no change)

- `app_shared.url_pattern.derive_url_pattern` / `URL_PATTERN_ALGORITHM_VERSION` (=1) — pattern + version
  (FR-001..FR-006, research D10).
- `app_shared.access.engine.next_attempt` / `resolve_effective_policy` — the default access ladder the
  consumption resolver overrides or falls back to (D6).
- `scrape_core.extraction.pipeline.extract` — the default extraction order (JSON-LD → CSS → regex …)
  reordered to preferred-first for learned domains (D6, §16).
- `scrape_core.pipelines.BatchedPersistencePipeline._flush_batch` — the off-reactor seam where
  `request_attempts` persist and where stats recording is added (D4).
- `apps/scrapers/price_monitor/spiders/generic_price_spider.load_targets` — the per-group resolution
  seam extended for profile get-or-create + learned-start (D5/D6).
- `app_shared.money.Money` / SPEC-06 validation — money/currency correctness gating a qualifying
  discovery/promotion success (Constitution VII).

## 10. Requirements → model coverage

| FR | Where satisfied |
|----|-----------------|
| FR-001..FR-004, FR-006 | reuse `url_pattern` (D10); `url_pattern`/`url_pattern_version` cols; override precedence |
| FR-005 | `STRATEGY_PATTERN_BACKFILL` maintenance task |
| FR-007, FR-027 | `domain_strategy_profiles` + unique `(ws, competitor, domain, url_pattern)` |
| FR-008 | `MethodType` + reused `AccessMethod`/`ExtractionMethod` (D1) |
| FR-009, FR-027 | `strategy_attempt_stats` + unique `(profile, method_type, method_name)` |
| FR-010, FR-011 | promotion evaluator (D9) + profile preferred/confidence/confirmed cols |
| FR-012 | `last_*_at` + `recent_failure_count` maintained at flush |
| FR-013..FR-015 | consumption resolver (D6), version-guarded, workspace-scoped |
| FR-016..FR-019 | discovery task + API + `strategy_discovery_runs` (D5/D7) |
| FR-020, FR-021 | rediscovery evaluator + inline + light re-check (D8) |
| FR-022..FR-025 | Redis buffer + atomic flush + combined reads + off-reactor recording (D4) |
| FR-026 | RLS on all three (transitive for stats, D3) |
| FR-028 | UUIDv7 via `Base` |
