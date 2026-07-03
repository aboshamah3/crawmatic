# Phase 0 Research: Current Prices & Alert Logic

All Technical-Context unknowns resolved doc-first against PROJECT_SPEC.md (§19, §22, §23,
§25, §26), the constitution, and the SPEC-07/08 code already in the repo. No open
NEEDS CLARIFICATION remains.

---

## D1 — Pure alert engine: where it lives and its exact shape

**Decision**: A framework-free package `libs/shared/app_shared/alerts/` (`engine.py`),
importing **only stdlib `decimal`** — no sqlalchemy, celery, fastapi, scrapy. It exposes:

- `filter_comparable(client_currency, rows) -> ComparableSplit` — pure partition of
  competitor rows into *included* (success ∧ comparable ∧ currency == client ∧ price not
  null) and *currency-mismatched* (currency present and ≠ client) ids. No DB.
- `discount_vs_average(average, client_price) -> Decimal` — `((avg - price)/avg)*100`,
  then `.quantize(Decimal("0.0001"), ROUND_HALF_UP)`.
- `decide(client_price, cheapest, average, highest, comparable_count) -> AlertType` — the
  ordered §23 tree (steps 1–8), operating on already-quantized `discount_vs_average`.
- `severity_for(alert_type) -> AlertSeverity` — the fixed FR-004/§23 map (no independent
  logic).
- `analyze(client_price, client_currency, competitor_rows) -> AlertOutcome` — the
  orchestrator that ties the above together and returns type, severity, benchmarks
  (cheapest/avg/highest — `None` when no comparables), `comparable_competitor_count`, the
  mismatched-competitor ids, a human message, and details dict.
- `transition(prev, new, *, had_history) -> AlertEventType | None` — the ordered
  event-transition rule (see D5).

**Rationale**: Principle VII demands "one ordered, deterministic computation"; Principle II
+ §26 demand the engine be independent of DB/Celery so it is exhaustively unit-testable
(every boundary) with no infra. Mirrors SPEC-08's pure `app_shared/jobs/*` (batching,
lifecycle, counters) that the Celery task merely orchestrates.

**Alternatives considered**: Putting the tree inside the Celery task (rejected — not
unit-testable without Celery, couples determinism to infra); a SQL-side computation
(rejected — floats/rounding non-determinism, unportable, untestable boundaries).

---

## D2 — Decimal determinism & boundary quantization

**Decision**: `discount_vs_average` is computed in `Decimal` and **explicitly quantized to
4 places with `ROUND_HALF_UP` before any comparison** (`Decimal("0.0001")`). Boundary
compares use `Decimal` literals: `> Decimal("5")`, `Decimal("1") <= x <= Decimal("5")`,
`Decimal("0") <= x < Decimal("1")`. NaN/Infinity are rejected at the boundary (reuse
`app_shared.money.parse_money` semantics — `is_finite()` guard, over-scale rejection).

**Rationale**: FR-008/FR-009 + §23 determinism rule + Edge Case "Decimal boundary
determinism". Without pre-compare quantization "exactly 1%" / "exactly 5%" stop being
testable under float drift. `Money()`/`parse_money` already reject NaN/Infinity/over-scale
(`libs/shared/app_shared/money.py`), so the engine reuses that contract for inputs.

**Boundary truth table (unit-tested exhaustively)**:

| discount_vs_average (quantized) | Alert type |
|---|---|
| exactly `0.0000` | CLOSE_TO_COMPETITORS |
| `> 0` and `< 1` | CLOSE_TO_COMPETITORS |
| exactly `1.0000` | NORMAL |
| `> 1` and `< 5` | NORMAL |
| exactly `5.0000` | NORMAL |
| `> 5` | CHANCE_TO_INCREASE_PRICE |

Steps 2–3 (`> highest` → RISK, `> cheapest` → HIGH_PRICE) run **before** the discount math,
so by step 4 `client_price <= cheapest <= average` ⇒ `discount_vs_average >= 0`; step 8
(`else → HIGH_PRICE`) is the documented unreachable defensive branch (degrade, never raise).

---

## D3 — `price_alert_events` partitioning (mirror SPEC-07 exactly)

**Decision**: `price_alert_events` is created **partitioned monthly by `created_at` from
birth**, with a composite `PRIMARY KEY (id, created_at)` (Postgres requires the partition
key be in the PK). The migration reproduces the SPEC-07 pattern verbatim:
`postgresql_partition_by="RANGE (created_at)"` on `op.create_table`, then **current + next
month** `CREATE TABLE … PARTITION OF … FOR VALUES FROM ('YYYY-MM-01') TO (...)` children via
`op.execute`, using a copied `_month_partition_bounds(now)` helper. RLS applied once to the
**parent** propagates to partitions.

**Rationale**: FR-003, §22 partitioned-table rules, §29, Principle VIII. The exact,
proven precedent is `alembic/versions/2db33dea5e14_observations_current_prices_tables.py`
(`price_observations`/`request_attempts`). The ORM mirrors `PriceObservation`: `created_at`
declared `primary_key=True` alongside inherited `id`; `__table_args__` carries the
`ForeignKeyConstraint` on `workspace_id` + `{"postgresql_partition_by": "RANGE (created_at)"}`.
Retention-by-drop / future-partition maintenance is **SPEC-15** (out of scope).

**Alternatives considered**: non-partitioned events table (rejected — violates §29/VIII for
an append-heavy history table); partition by `updated_at`/month-of-alert (rejected — events
are immutable and keyed on their own `created_at`).

---

## D4 — `price_analysis` task, its queue, and per-variant-per-job dedup

**Decision**: A new Celery task `price_analysis.recompute_variant`
(`app_shared.task_names.PRICE_ANALYSIS_RECOMPUTE`) on its **own `price_analysis` queue**
(registered in `apps/workers/app/workers/celery_app.py` `task_queues` + `task_routes`),
in a new `apps/workers/app/workers/tasks_analysis.py`. kwargs:
`workspace_id`, `product_variant_id`, optional `product_id`, optional `scrape_job_id`.

**Dedup per variant per job** is realized on the **emission side** with the existing Redis
`SET NX` idempotency approach (same primitive as `scrapyd/client.py` dispatch-key and
SPEC-08 stall-window keys): before enqueuing, claim
`analysis:enqueued:{scrape_job_id}:{product_variant_id}` with `set(key, "1", nx=True, ex=TTL)`;
enqueue **only if claimed**. Many completed matches of one variant in one job therefore
collapse to a single enqueue → single recompute → no hot-row contention (§26, VIII).
The client-price-change trigger has **no job** (`scrape_job_id=None`) → enqueue directly
(user-driven, low frequency, no dedup key needed).

**Idempotency is the correctness guarantee, not the dedup key**: the task is fully
idempotent (FR-014) — re-running with unchanged inputs produces byte-identical state and
writes **no** duplicate event — so at-least-once delivery is always safe; the `SET NX` key
is a *contention reducer*, not a correctness guard (if it expires/fails, correctness still
holds).

**Rationale**: §26 (`price_analysis` queue, one task per variant per job, idempotent),
Principle V (analysis out of the reactor), Principle VIII (no hot-row contention). Reuses
the repo's established `SET NX` idempotency primitive rather than inventing new machinery.

**Alternatives considered**: DB advisory lock per variant (rejected — serializes under
PgBouncer, and idempotency already makes duplicates harmless); a dedup SET keyed only by
variant (rejected — would suppress a legitimately-needed later recompute in a different job);
Celery `task_id` dedup (rejected — not job-scoped, weaker than an explicit key).

---

## D5 — Event-transition rule (the ordered map)

**Decision**: Persist a `price_alert_events` row **only when the alert `type` OR `severity`
changes**. Given previous `(prev_type, prev_severity)` (None if no prior state) and newly
computed `(new_type, new_severity)`, resolve in this order (Clarifications / FR-013):

1. `prev is None` ∧ `new` is NORMAL/NONE → **no event** (state created, no history).
2. `prev is None` ∧ `new` non-NORMAL → **CREATED**.
3. `prev == new` (same type ∧ same severity) → **no persisted event** (UNCHANGED); advance
   `last_seen_at` only.
4. `prev` non-NORMAL ∧ `new` NORMAL/NONE → **RESOLVED** (set `resolved_at`, status RESOLVED).
5. `prev` was NORMAL/resolved ∧ `new` non-NORMAL ∧ a prior alert history exists → **REOPENED**.
6. `prev` non-NORMAL ∧ `new` a different non-NORMAL (type change, or same-type severity
   change) → **UPDATED**.

`UNCHANGED` is a defined `event_type` in the vocabulary but is **never persisted** per
unchanged run (avoids history spam + hot-row contention). Severity is derived from type
(FR-011), so a type change is the primary trigger; a same-type severity change still yields
UPDATED. `had_history` = "a prior `variant_alert_states` row exists / a prior non-NORMAL
alert was recorded" — distinguishes CREATED (step 2) from REOPENED (step 5).

**Rationale**: Clarifications session pinned this exact ordering; FR-013 + US2 acceptance
scenarios; §26 "must not be incremented per run" (no history spam). Pure and unit-testable
as a transition table.

---

## D6 — Currency-mismatch handling & side effect

**Decision**: A competitor `match_current_prices` row contributes to comparison **iff**
`success=true ∧ comparable=true ∧ currency == client_currency ∧ price is not null`
(FR-010, §23). A row whose `currency` is present and **differs** from the client currency is
excluded, and the task **marks that `match_current_prices.comparable=false` and sets
`error_code=CURRENCY_MISMATCH`** (existing `ScrapeErrorCode.CURRENCY_MISMATCH`,
`libs/shared/app_shared/enums.py`). No FX conversion in v1.

**Rationale**: §19, §23, Principle VII, Edge Case "Currency mismatch". The *decision* of
which rows to exclude is pure (engine `filter_comparable` returns the mismatched ids); the
*write-back* (flip `comparable`, stamp `CURRENCY_MISMATCH`) is a DB side effect performed by
the task via a scoped `UPDATE`. Keeping the classification in the engine keeps it testable;
keeping the write in the task keeps the engine DB-free.

---

## D7 — Recompute trigger wiring (three sources, one task)

**Decision**: All three FR-015 triggers route to the same idempotent task:

- **(a) Scrape completion** — wired into the existing `scrape_core/pipelines.py`
  `_flush_batch` seam. That function already, *after the persistence transaction commits*,
  enqueues `SCRAPE_FINALIZE_JOBS` per distinct affected job. SPEC-09 adds: after the same
  commit, for each **distinct `(workspace_id, scrape_job_id, product_variant_id)`** in the
  batch, claim the `SET NX` dedup key and, if won, `enqueue(PRICE_ANALYSIS_RECOMPUTE,
  queue="price_analysis", kwargs={...})`. Emission is by name → `scrape-core` stays
  import-clean (it already imports `app_shared.messaging.enqueue`). Dedup ⇒ one recompute
  per variant per job even across many flush batches / many matches.
- **(b) Client price/currency change** — the API `PATCH /v1/variants/{id}` handler and the
  variant bulk-upsert handler enqueue `PRICE_ANALYSIS_RECOMPUTE` (job_id=None) **only when
  `current_price` or `currency` actually changed**, via `app_shared.messaging.enqueue` (API
  never imports `apps/workers`). Reflected immediately, no scrape (FR-016, §25 "client price
  update").
- **(c) Match archived/paused** — the match status-change path (comparable set changed)
  enqueues the same task for the affected variant. (Match status mutation lives in the
  matches surface; SPEC-09 adds the enqueue hook there, same by-name seam.)

**Rationale**: §23 recompute triggers, §25 job flow + client-price-update flow, Principle
I/V (by-name enqueue seam), Principle VIII (dedup on the scrape path). Reuses the exact
post-commit emission point SPEC-08 already established in `_flush_batch`.

**Alternatives considered**: emitting from `finalize_jobs` (rejected — per-job, not
per-variant, and would need to re-derive affected variants; the pipeline already has them in
hand); emitting from inside the spider before commit (rejected — reactor-blocking Redis +
could enqueue before the row is durable).

---

## D8 — New enums

**Decision**: Add four `StrEnum`s to `app_shared/enums.py` (string-backed VARCHAR via
`enum_column`, never DB-native enums — repo convention):

- `AlertType`: `NO_COMPETITOR_DATA, RISK, HIGH_PRICE, CHANCE_TO_INCREASE_PRICE, NORMAL,
  CLOSE_TO_COMPETITORS`
- `AlertSeverity`: `NONE, LOW, MEDIUM, HIGH, CRITICAL`
- `AlertStatus`: `ACTIVE, RESOLVED`
- `AlertEventType`: `CREATED, UPDATED, RESOLVED, REOPENED, UNCHANGED`

**Rationale**: FR-004 vocabularies, §22/§23. Matches the `ScrapeJobStatus`/`ScrapeTargetStatus`
precedent (SPEC-08) — app-validated strings, portable, no ALTER TYPE migrations.

---

## D9 — Read endpoints, scope, and pagination

**Decision**: Four workspace-scoped, scope-gated read endpoints (FR-017..020), all gated by
the **already-existing `Scope.ALERTS_READ` (`alerts:read`)** — no new scope, no scope
migration:

- `GET /v1/variants/{variant_id}/price-comparison` (on `routers/variants.py`) — single
  variant; 404 unknown/cross-workspace.
- `GET /v1/alerts/current` — cursor-paginated list of `variant_alert_states`, filterable by
  `type` and `severity`.
- `GET /v1/alerts/current/{variant_id}` — the current alert state for one variant.
- `GET /v1/alert-events` — cursor-paginated `price_alert_events` history, filterable by
  `variant_id`.

Pagination reuses `app_shared.pagination` (`clamp_limit`, `decode_cursor`,
`keyset_predicate`, `paginate`) exactly as `routers/matches.py`. That helper keys the cursor
on **`(created_at, id)`** — so both paginated tables carry a `created_at` column (see D10).

**Rationale**: FR-017..020, §24 (`/v1`, cursor pagination default 50 / max 500), Principle
II. `alerts:read` already exists in `app_shared.security.scopes.Scope`; price-comparison
surfaces the alert type/severity so it belongs on the same read scope.

**Alternatives considered**: `results:read` for price-comparison (viable; rejected for
uniformity — one read scope across the SPEC-09 surface is simpler and price-comparison is
alert-adjacent); a new `alert_events:read` scope (rejected — unnecessary granularity, no
spec requirement).

---

## D10 — `created_at` on the current-state tables (pagination + convention)

**Decision**: Give `variant_price_states` and `variant_alert_states` a `created_at` column
(via `TimestampMixin`, which supplies `created_at` + `updated_at`) in addition to their §22
custom timestamps (`calculated_at`; `first_seen_at`/`last_seen_at`/`resolved_at`). §22 lists
these tables' timestamps as `…/updated_at` and `…/updated_at` without enumerating
`created_at`, but the established repo precedent for current-state tables
(`MatchCurrentPrice` uses `TimestampMixin` → `created_at`+`updated_at` even though §22's
`match_current_prices` block lists only `scraped_at`+`updated_at`) is to carry `created_at`.
It is a **benign superset** of the §22 shape and gives `GET /v1/alerts/current` a stable
`(created_at, id)` keyset cursor with the shared pagination helper (unchanged).

`price_alert_events` carries `created_at` **only** (append-only, no `updated_at`) — it is the
partition key and PK part, declared explicitly (not via `TimestampMixin`), mirroring
`PriceObservation.scraped_at`.

**Rationale**: Lets all pagination reuse `app_shared.pagination` verbatim (no signature
change to shared code), stays consistent with `MatchCurrentPrice`, and keeps `updated_at`
(§22-required) present. Documented as a deliberate, precedent-backed superset.

---

## D11 — Missing client price (defensive)

**Decision**: `product_variants.current_price` is `NOT NULL` (catalog model), so a client
price is always present in practice. Defensively, if a variant is ever not analyzable (no
client price), the task records a NO_COMPETITOR_DATA-equivalent "not analyzable" state
rather than raising (Assumptions / Edge Case "Client price missing"). No divide-by-zero:
with zero comparables the engine returns NO_COMPETITOR_DATA before any average is computed.

**Rationale**: Assumptions + Edge Cases; robustness without new schema.

---

## Resolved unknowns summary

| Unknown | Resolution |
|---|---|
| Engine location & purity | `app_shared/alerts/engine.py`, stdlib-decimal only (D1) |
| Boundary determinism | Decimal quantize 4dp ROUND_HALF_UP pre-compare (D2) |
| Events partitioning | monthly by `created_at`, PK `(id, created_at)`, SPEC-07 pattern (D3) |
| Task/queue/dedup | `price_analysis` queue; Redis `SET NX` per `(job, variant)` (D4) |
| Event-transition rule | ordered 6-case map, UNCHANGED never persisted (D5) |
| Currency mismatch | exclude + `comparable=false` + `CURRENCY_MISMATCH`, no FX (D6) |
| Trigger wiring | `_flush_batch` (a), variant PATCH/bulk (b), match status (c) (D7) |
| Enums | 4 new `StrEnum`s (D8) |
| Endpoints/scope/pagination | 4 reads, `alerts:read`, shared cursor helper (D9) |
| `created_at` for pagination | `TimestampMixin` on current-state tables (D10) |
| Missing client price | not-analyzable state, no raise (D11) |

No open NEEDS CLARIFICATION.
