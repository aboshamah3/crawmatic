# Quickstart & Validation: Current Prices & Alert Logic (SPEC-09)

Runnable validation scenarios proving the feature end-to-end. Pure-engine scenarios run
with **no infrastructure**; DB/Redis/Celery/API scenarios are integration tests that **skip
cleanly** when infra is absent (SPEC-01..08 convention).

## Prerequisites

```bash
uv sync --all-packages          # workspace deps (only if not already synced)
```

No Docker/Postgres/Redis/Celery/Scrapyd is required for the engine tests. Integration tests
detect missing infra and skip.

## 1. Pure engine — determinism & boundaries (no infra) — SC-001

```bash
uv run pytest -q libs/shared/tests/unit/test_alert_engine.py
```

Expected: every §23 branch (1–8) and every boundary maps exactly:

| Scenario | client vs comps | Expected type / severity |
|---|---|---|
| Above all | price > highest | RISK / CRITICAL |
| Above cheapest | cheapest < price ≤ highest | HIGH_PRICE / HIGH |
| Exactly 1% below avg | discount == 1.0000 | NORMAL / NONE |
| Exactly 5% below avg | discount == 5.0000 | NORMAL / NONE |
| >5% below avg | discount > 5 | CHANCE_TO_INCREASE_PRICE / MEDIUM |
| 0% / <1% below avg | 0 ≤ discount < 1 | CLOSE_TO_COMPETITORS / MEDIUM |
| No comparables | count == 0 | NO_COMPETITOR_DATA / LOW |

Also asserted: Decimal quantization `ROUND_HALF_UP` at the 4th place; NaN/Infinity/over-scale
rejected; severity map total; re-running `analyze` on identical inputs is byte-identical.

## 2. Event-transition rule (no infra) — SC-004

```bash
uv run pytest -q libs/shared/tests/unit/test_alert_transitions.py
```

Truth table: `None→NORMAL` ⇒ no event; `None→non-NORMAL` ⇒ CREATED; same ⇒ None (UNCHANGED);
`non-NORMAL→NORMAL` ⇒ RESOLVED; `NORMAL→non-NORMAL (had history)` ⇒ REOPENED;
`non-NORMAL→different non-NORMAL` ⇒ UPDATED.

## 3. Migration & single head (skip-clean) — FR-006

```bash
uv run alembic heads          # exactly one head after the new revision
uv run pytest -q -k migration_alerts   # upgrade→downgrade, partitions, RLS present+forced
```

Expected: `variant_price_states`, `variant_alert_states`, `price_alert_events` created;
`price_alert_events` is `PARTITION BY RANGE (created_at)` with current + next-month
partitions; RLS ENABLE+FORCE on all three; one alembic head preserved (chains onto
`a6b0234cd4ad`).

## 4. `price_analysis` task — upsert, idempotency, currency mismatch (skip-clean) — SC-002/006/007

```bash
uv run pytest -q -k price_analysis_task
```

- Seed a variant + several comparable `match_current_prices`; run the task; assert
  `variant_price_states` benchmarks/count/type and `variant_alert_states` match the engine.
- A competitor in a non-matching currency is flipped `comparable=false` +
  `CURRENCY_MISMATCH` and excluded from benchmarks (SC-006).
- Run the task twice with unchanged inputs ⇒ identical state, **zero** duplicate events
  (idempotent).
- NORMAL → HIGH_PRICE → NORMAL sequence ⇒ exactly one CREATED, one RESOLVED; an unchanged
  re-run advances `last_seen_at` only.

## 5. Recompute triggers (skip-clean) — SC-003/007

```bash
uv run pytest -q -k recompute_triggers
```

- Simulate N match completions of one variant in one job (fake Redis honoring `SET NX`) ⇒
  exactly **one** `PRICE_ANALYSIS_RECOMPUTE` enqueued for that variant/job (SC-007).
- PATCH a variant's `price`/`currency` ⇒ one recompute enqueued (SC-003); PATCH only
  `title` ⇒ none. Archive a match ⇒ one recompute for its variant.

## 6. Read endpoints & workspace isolation (skip-clean) — SC-005/008, FR-017..020

```bash
uv run pytest -q -k api_alerts
```

- `GET /v1/variants/{id}/price-comparison` returns client price/currency, cheapest/average/
  highest, comparable count, and current alert type/severity consistent with the stored
  state (SC-005); unknown/cross-workspace/never-analyzed → 404.
- `GET /v1/alerts/current` pages and filters by type/severity; `GET
  /v1/alerts/current/{variant_id}` returns one; `GET /v1/alert-events` pages and filters by
  variant.
- **Isolation**: workspace A never observes B's price state / alert state / events; a
  no-workspace-context read yields zero rows; missing `alerts:read` scope → 403 (SC-008).

## Full suite

```bash
uv run pytest -q         # engine unit tests pass with zero infra; infra-bound tests skip cleanly
```

## Cross-reference

- Decision tree / boundaries / severity: `contracts/alert-engine.md`.
- Table shapes: `data-model.md`, `contracts/models-alerts.md`.
- Migration/partitioning: `contracts/migration-alerts.md`.
- Task/queue/dedup: `contracts/price-analysis-task.md`.
- Trigger wiring: `contracts/recompute-triggers.md`.
- Endpoints: `contracts/api-alerts.md`.
