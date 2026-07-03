# Feature Specification: Current Prices & Alert Logic

**Feature Branch**: `009-current-prices-alerts`

**Created**: 2026-07-03

**Status**: Draft

**Input**: User description: "Current Prices & Alert Logic (SPEC-09). Turn per-match observations into variant-level price comparison and a deterministic alert state."

## Clarifications

### Session 2026-07-03

- Q: What is the `variant_alert_states.status` vocabulary (doc lists the field, not values)? → A: `ACTIVE` / `RESOLVED` — the state is ACTIVE while its type is non-NORMAL and RESOLVED (with resolved_at set) when the variant returns to NORMAL/NONE.
- Q: Is a `price_alert_events` row written on every analysis, including when nothing changed? → A: No. An event row is persisted **only when the alert type or severity changes**; on an unchanged run no event is written and the state's `last_seen_at` advances. (UNCHANGED is a defined event_type in the vocabulary but is not persisted per unchanged run — it avoids history spam and hot-row contention.)
- Q: What is the exact event-transition rule (given previous (type,severity) `prev` and newly computed `new`)? → A: Ordered: (1) `prev is None` and `new` is NORMAL/NONE → no event; (2) `prev is None` and `new` non-NORMAL → **CREATED**; (3) `prev == new` → no persisted event (UNCHANGED), advance last_seen_at; (4) `prev` non-NORMAL and `new` NORMAL/NONE → **RESOLVED** (set resolved_at); (5) `prev` was resolved/NORMAL and `new` non-NORMAL, with a prior alert history → **REOPENED**; (6) `prev` non-NORMAL and `new` a different non-NORMAL → **UPDATED**. Severity is derived from type (FR-011), so a type change is the primary trigger; a same-type severity change also triggers UPDATED.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Deterministic variant alert after a scrape (Priority: P1)

After a scrape job finishes for a product variant's competitor matches, the system recomputes that variant's price position against its comparable competitors and records a single, deterministic alert state (e.g. RISK, HIGH_PRICE, NORMAL). The operator can then fetch the variant's price comparison and see the client price, the competitor benchmarks (cheapest/average/highest), how many comparable competitors contributed, and the current alert type/severity.

**Why this priority**: This is the whole point of the product — converting raw scraped prices into an actionable pricing signal. It is the acceptance core ("variant price state updated", "alert state deterministic", "comparison API returns expected result") and everything else builds on the analysis engine.

**Independent Test**: Seed a variant with a client price and several matches whose current prices are known; run the analysis for that variant; assert the variant price state carries the correct cheapest/average/highest benchmarks and comparable count, the alert type matches the ordered decision tree for those inputs, and the comparison endpoint returns those values. Re-run with identical inputs and assert byte-identical state (determinism).

**Acceptance Scenarios**:

1. **Given** a variant priced above every comparable competitor, **When** analysis runs, **Then** the alert type is RISK (severity CRITICAL) and the variant price state records the benchmarks and comparable count.
2. **Given** a variant priced exactly 1% below the average competitor price, **When** analysis runs, **Then** the alert type is NORMAL (the boundary is deterministic under Decimal quantization).
3. **Given** a variant priced exactly 5% below average, **When** analysis runs, **Then** the alert type is NORMAL; **Given** priced more than 5% below average, **Then** CHANCE_TO_INCREASE_PRICE.
4. **Given** a variant with no comparable competitor prices, **When** analysis runs, **Then** the alert type is NO_COMPETITOR_DATA (severity LOW) and benchmarks are null with comparable count 0.
5. **Given** a completed variant analysis, **When** I GET the variant price-comparison endpoint, **Then** I receive client price/currency, cheapest/average/highest competitor price, comparable_competitor_count, and current alert type/severity, scoped to my workspace.

---

### User Story 2 - Alert history when the signal changes (Priority: P2)

When a variant's alert type or severity changes between analyses, the system records an event in the alert history (created/updated/resolved/reopened); when nothing changes, no spurious history is written. The operator can page through the alert-event history and the current alert list for the workspace.

**Why this priority**: Auditable history of when a pricing situation appeared, worsened, or cleared is what makes alerts trustworthy and actionable over time. It depends on the P1 analysis engine producing alert states, so it comes second.

**Independent Test**: Drive a variant through a sequence of analyses that changes its alert type (e.g. NORMAL → HIGH_PRICE → NORMAL), asserting exactly one CREATED, one UPDATED/RESOLVED per real transition, and zero events when an analysis leaves the type/severity unchanged; then page the alert-events and current-alerts endpoints.

**Acceptance Scenarios**:

1. **Given** a variant with no prior alert state, **When** its first non-NORMAL alert is computed, **Then** an alert state is created and a CREATED event is recorded.
2. **Given** an existing alert whose type/severity changes, **When** analysis runs, **Then** an UPDATED event records previous and new type/severity.
3. **Given** an existing non-NORMAL alert that returns to NORMAL/NONE, **When** analysis runs, **Then** the alert is marked resolved (resolved_at set) and a RESOLVED event is recorded; a later re-departure from NORMAL records a REOPENED event.
4. **Given** an analysis that yields the same type and severity as the current state, **When** it runs, **Then** no new event is written (or an UNCHANGED event per the defined rule) and the state's last_seen_at advances.
5. **Given** alert history exists, **When** I GET the alert-events endpoint, **Then** I receive a paginated, workspace-scoped list filterable by variant; **When** I GET the current-alerts endpoint, **Then** I receive the current alert state per variant, filterable by type/severity.

---

### User Story 3 - Client price change reflected immediately (Priority: P3)

When a client changes a variant's own price or currency (via variant update or bulk upsert), the variant's alert state is recomputed immediately using the existing competitor prices — without waiting for the next scrape. Analysis triggered many times for the same variant within one scrape job collapses into a single recompute rather than contending on the variant's state row.

**Why this priority**: Keeps the pricing signal correct the moment the client acts, and protects the hot variant-state row from write contention at scale. It reuses the same analysis engine and trigger, so it follows the core.

**Independent Test**: Update a variant's client price via the catalog path and assert a recompute is enqueued (idempotent/deduplicated) that produces a new alert state reflecting the new client price against the unchanged competitor set, with no scrape involved; separately, simulate many match completions for one variant in one job and assert the recompute is deduplicated to a single execution per variant per job.

**Acceptance Scenarios**:

1. **Given** a variant with an existing alert state, **When** the client raises its price above all competitors, **Then** a recompute runs (no scrape) and the alert becomes RISK immediately.
2. **Given** a variant whose currency is changed, **When** analysis runs, **Then** competitors whose currency no longer matches are excluded and marked comparable=false with a CURRENCY_MISMATCH note.
3. **Given** N matches of one variant completing in a single scrape job, **When** completions are processed, **Then** the per-variant recompute is deduplicated per variant per job (a single analysis execution for that variant in that job).
4. **Given** the same analysis inputs, **When** the task runs more than once, **Then** the resulting state is identical and no duplicate events are produced (idempotent).

---

### Edge Cases

- **Currency mismatch**: A competitor match whose current-price currency differs from the client variant currency is excluded from comparison, its match current price is marked comparable=false, and a CURRENCY_MISMATCH warning/error is stored. No cross-currency comparison happens in v1 (no FX conversion).
- **No comparable competitors**: Yields NO_COMPETITOR_DATA with null benchmarks and comparable_competitor_count = 0; must not divide by zero.
- **Decimal boundary determinism**: discount_vs_average is computed in Decimal, quantized to 4 places ROUND_HALF_UP before any comparison, so "exactly 1%" and "exactly 5%" land on their defined types every time (never float drift).
- **Defensive branch**: Once steps 2–3 pass, discount_vs_average is ≥ 0, so decision-tree step 8 is unreachable; unexpected/degenerate data degrades to HIGH_PRICE instead of raising.
- **Unchanged re-run**: Re-running analysis with identical inputs produces identical state and does not spam alert history.
- **Cross-workspace isolation**: A caller can never read another workspace's price state, alert state, or events; no-workspace-context reads return zero rows.
- **Dangling soft references**: latest_alert_state_id and observation soft references may point at rows in dropped partitions after retention (SPEC-15); readers tolerate this because the current-state rows carry every field analysis and the comparison endpoint need.
- **Client price missing**: A variant with no client price cannot be positioned; analysis records NO_COMPETITOR_DATA-equivalent / a defined "not analyzable" state rather than crashing (documented in Assumptions).

## Requirements *(mandatory)*

### Functional Requirements

**Data model**

- **FR-001**: System MUST persist `variant_price_states` (one per workspace+variant, unique(workspace_id, product_variant_id)) carrying client_price, currency, cheapest/average/highest competitor price (nullable), comparable_competitor_count, latest_alert_type, latest_alert_severity, latest_alert_state_id (nullable), calculated_at, updated_at — with the exact PROJECT_SPEC §22 shape.
- **FR-002**: System MUST persist `variant_alert_states` (one per workspace+variant, unique(workspace_id, product_variant_id)) carrying type, severity, status, client_price, benchmark_price (nullable), cheapest/average competitor price (nullable), message, details (json, nullable), first_seen_at, last_seen_at, resolved_at (nullable), updated_at — exact §22 shape.
- **FR-003**: System MUST persist `price_alert_events` (append-only history) carrying alert_state_id, event_type, previous/new type, previous/new severity, message, details (json, nullable), created_at — exact §22 shape — created **partitioned monthly by created_at from birth**, with the partition key included in the primary key, following the Section 22 partitioned-table rules and the existing partitioned-table precedent.
- **FR-004**: Alert `type` MUST be one of NO_COMPETITOR_DATA, RISK, HIGH_PRICE, CHANCE_TO_INCREASE_PRICE, NORMAL, CLOSE_TO_COMPETITORS. Severity MUST map exactly: NO_COMPETITOR_DATA=LOW, RISK=CRITICAL, HIGH_PRICE=HIGH, CHANCE_TO_INCREASE_PRICE=MEDIUM, NORMAL=NONE, CLOSE_TO_COMPETITORS=MEDIUM. Event `event_type` MUST be one of CREATED, UPDATED, RESOLVED, REOPENED, UNCHANGED.
- **FR-005**: All three tables MUST carry workspace_id and enforce workspace isolation via application scoping AND Postgres RLS, consistent with SPEC-03..08; no-workspace-context reads MUST return zero rows.
- **FR-006**: Schema changes MUST be delivered as a forward migration composing with the existing single-head chain and continuing to yield a single head; the partitioned events table plus its initial partitions must be created in that migration.

**Analysis engine (pure, deterministic)**

- **FR-007**: The alert decision MUST follow the ordered §23 tree exactly: (1) no comparable prices → NO_COMPETITOR_DATA; (2) client_price > highest → RISK; (3) client_price > cheapest → HIGH_PRICE; (4) compute discount_vs_average = ((average − client_price) / average) × 100; (5) >5 → CHANCE_TO_INCREASE_PRICE; (6) 1..5 inclusive → NORMAL; (7) 0..<1 → CLOSE_TO_COMPETITORS; (8) defensive else → HIGH_PRICE.
- **FR-008**: discount_vs_average MUST be computed with Decimal arithmetic and explicitly quantized to 4 decimal places using ROUND_HALF_UP **before** any boundary comparison — never binary float. Prices MUST use Decimal/NUMERIC(18,4) throughout; NaN/Infinity are rejected at the boundary.
- **FR-009**: Boundary behavior MUST be: exactly 0% below → CLOSE_TO_COMPETITORS; >0% and <1% → CLOSE_TO_COMPETITORS; exactly 1% → NORMAL; exactly 5% → NORMAL; >5% → CHANCE_TO_INCREASE_PRICE.
- **FR-010**: A competitor is included in comparison only when its match current price has success=true AND comparable=true AND currency = client currency AND price is not null. Any competitor whose currency differs from the client currency MUST be excluded, its match current price marked comparable=false, and a CURRENCY_MISMATCH warning/error stored. No cross-currency comparison (no FX) in v1.
- **FR-011**: The severity for a computed type MUST be assigned solely from the FR-004 mapping (no independent severity logic).

**Recompute task & lifecycle**

- **FR-012**: A `price_analysis` task MUST run in a background worker on its own queue, **separate from the spider/reactor**, be variant-level, idempotent, and deduplicated per variant per job (many completed matches of one variant in one job collapse into a single recompute so they do not contend on the variant state rows).
- **FR-013**: `price_analysis` MUST upsert `variant_price_states` (benchmarks, comparable_competitor_count, latest_alert_type/severity, calculated_at) and upsert `variant_alert_states` (status ACTIVE while type non-NORMAL, RESOLVED with resolved_at when back to NORMAL/NONE), and MUST write a `price_alert_events` row **only when the alert type or severity changes**, per the ordered transition rule in Clarifications: CREATED (first non-NORMAL), UPDATED (non-NORMAL → different non-NORMAL, or same-type severity change), RESOLVED (→ NORMAL/NONE), REOPENED (leaving NORMAL after a prior resolution); an unchanged run writes no event and advances last_seen_at — maintaining first_seen_at/last_seen_at/resolved_at accordingly.
- **FR-014**: Re-running `price_analysis` with unchanged inputs MUST produce identical state and MUST NOT create duplicate events (full idempotency).
- **FR-015**: The system MUST trigger `price_analysis` for a variant from each of these, all routed to the same idempotent, deduplicated task: (a) a scrape completed for a match of the variant — emitted after persistence, deduplicated per variant per job, wired from the SPEC-07/08 scrape-completion path; (b) the variant's client price or currency changed via variant update or bulk upsert — reflected immediately without waiting for a scrape; (c) a match of the variant archived/paused (comparable set changed).
- **FR-016**: A client price/currency change MUST be reflected in the variant's alert state without waiting for the next scrape.

**Read endpoints (API service, workspace-scoped, scope-gated)**

- **FR-017**: System MUST expose `GET /v1/variants/{variant_id}/price-comparison` returning client price/currency, cheapest/average/highest competitor price, comparable_competitor_count, and current alert type/severity for the variant, scoped to the caller's workspace (404 for unknown/cross-workspace variant).
- **FR-018**: System MUST expose `GET /v1/alerts/current` (paginated list of current variant alert states, filterable by type and severity) and `GET /v1/alerts/current/{variant_id}` (the current alert state for one variant), workspace-scoped.
- **FR-019**: System MUST expose `GET /v1/alert-events` returning a paginated, workspace-scoped alert-event history, filterable by variant.
- **FR-020**: All new endpoints MUST require the appropriate read scope and enforce workspace scoping + RLS on every read.

### Key Entities *(include if feature involves data)*

- **VariantPriceState**: The current, computed pricing position of one variant vs its comparable competitors — client price, competitor benchmarks (cheapest/average/highest), comparable competitor count, and a pointer to the latest alert type/severity/state. One per workspace+variant.
- **VariantAlertState**: The current alert for one variant — type, severity, status, the prices that justified it, a human-readable message, and lifecycle timestamps (first_seen/last_seen/resolved). One per workspace+variant.
- **PriceAlertEvent**: An append-only, monthly-partitioned record of each alert transition (created/updated/resolved/reopened/unchanged) with previous and new type/severity. Many per variant over time.
- **price_analysis (task)**: The single idempotent, per-variant, per-job-deduplicated unit of recompute that reads the variant + its comparable match current prices, runs the decision tree, and writes the three tables. Not an entity, but the central behavioral unit.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For any fixed set of client price and comparable competitor prices, the computed alert type is identical on every run (deterministic), including the exact 0% / 1% / 5% boundaries.
- **SC-002**: After a scrape completes for a variant's matches, the variant's price state and alert state reflect the new competitor prices (recompute is triggered, not skipped).
- **SC-003**: A client price or currency change is reflected in the variant's alert state without any scrape occurring.
- **SC-004**: An alert-history event is written exactly when the alert type or severity changes, and not when it is unchanged.
- **SC-005**: The variant comparison endpoint returns client price, cheapest/average/highest competitor benchmarks, comparable count, and current alert type/severity consistent with the stored state.
- **SC-006**: Competitors in a non-matching currency never affect a variant's benchmarks or alert, and are recorded as CURRENCY_MISMATCH.
- **SC-007**: Many match completions for one variant within one job produce a single recompute for that variant (deduplicated), so the variant state row is not written once per match.
- **SC-008**: A caller can never observe another workspace's price state, alert state, or events; no-workspace-context reads yield zero rows.

## Assumptions

- **Scope**: Only the variant-level analysis layer + the three new tables + the four read endpoints + the recompute wiring are in scope. `variant_price_daily_rollups` and retention/rollup/partition-maintenance jobs are SPEC-15; `webhook_endpoints`/`webhook_events` and WebhookEvent emission are SPEC-16 (this spec stops at persisting `price_alert_events`); access policies/proxies/request-attempt logic are SPEC-10; rate limiting / in-flight locks are SPEC-11; the scheduler is SPEC-13.
- **Reuse of existing tables**: `price_observations`, `match_current_prices`, and `request_attempts` already exist (SPEC-07); the item pipeline already upserts `match_current_prices` for successful comparable items. This spec reads `match_current_prices` for competitor prices and reads `product_variants.current_price`/`currency` for the client price; it does not re-implement observation persistence.
- **Deferred endpoints**: `GET /v1/products/{product_id}/price-comparison` (product-level), `GET /v1/matches/{match_id}/current-price`, `GET /v1/observations`, and `PATCH /v1/alerts/current/{variant_id}` (alert acknowledge) are related but not required by this spec's acceptance; they are deferred unless trivially derivable during planning. The binding endpoints are FR-017…FR-019.
- **Alert-state status**: `variant_alert_states.status` ∈ {ACTIVE, RESOLVED} — ACTIVE while type is non-NORMAL, RESOLVED (resolved_at set) when the variant returns to NORMAL/NONE (pinned in Clarifications).
- **Dedup mechanism**: "Deduplicated per variant per job" is realized with an idempotency key over (variant, job) using the existing Redis-based dedup/idempotency approach from prior specs; exact key/storage is an implementation choice for planning, the binding requirement is single-recompute-per-variant-per-job.
- **Missing client price**: A variant lacking a client price is recorded in a defined non-analyzable state (treated like NO_COMPETITOR_DATA for comparison purposes) rather than raising.
- **Deferred live verification**: No running Docker/Postgres/Redis/Celery/Scrapyd in the build environment. The decision tree, currency filtering, event-transition logic, severity mapping, and dedup key are pure/DB-independent and are exhaustively unit-tested (every §23 boundary value); DB/Redis-dependent behaviors use integration tests that skip cleanly, consistent with SPEC-01..08.
- **ID/timestamp/money conventions**: uuidv7 primary keys, NUMERIC(18,4) money, and standard timestamp conventions follow the project-wide strategy already established.
