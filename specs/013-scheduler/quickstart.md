# Quickstart & Validation: SPEC-13 Scheduler

Validation guide only — implementation details live in `data-model.md`, `contracts/`, and
`tasks.md`. Two tiers, matching specs 05–12:

- **Unit tests** (`tests/unit/`) — pure logic, run everywhere (no DB): cadence math, scope→match
  predicate selection, claim/enqueue ordering (with a fake session), validation, import boundary.
- **Live-DB integration tests** (`tests/integration/*_live.py`) — SKIP-LOCKED concurrency, RLS
  denial, alembic upgrade/downgrade, end-to-end pass. Guarded by the standard per-file
  `pytestmark = pytest.mark.skipif(not _live_refresh_rules_reachable(), reason=...)` probe (copy the
  `test_competitors_crud_live.py` idiom, swapping the probed table to `refresh_rules`). They skip
  cleanly when no Postgres is reachable (this build env has no live DB).

## Prerequisites

```bash
cd /srv/crawmatic/crawmatic
uv sync --all-packages          # NB: always --all-packages (workspace member deps)
```
`croniter` is added to `libs/shared/pyproject.toml`; the sync pulls it in for `app_shared` (and
transitively the scheduler + API).

## Run the tests

```bash
uv run pytest tests/unit -q                       # all pass (no DB needed)
uv run pytest tests/integration -q                # live tests skip without Postgres
# with a live DB + migrations applied:
uv run alembic upgrade head                        # applies the refresh_rules migration
uv run pytest tests/integration -q                # live tests execute
```

## Scenario 1 — Configure rules via API (US1 / SC-001)

1. `POST /v1/refresh-rules` `{name, scope:"WORKSPACE", cron_expression:"0 6 * * *"}` → `201`,
   `enabled=true`, `next_run_at` = next 06:00 UTC.
2. `POST /v1/refresh-rules` `{name, scope:"PRODUCT_GROUP", product_group_id:<id>, interval_minutes:60}`
   → `201`, `next_run_at ≈ now + 60m`.
3. `GET /v1/refresh-rules` returns both; `PATCH {enabled:false}` on one persists it disabled.
4. Negative: neither/both cadence → `422 INVALID_CADENCE`; `scope:"MATCH"` without `match_id` →
   `422 SCOPE_TARGET_MISMATCH`; bad cron → `422 INVALID_CRON`.

## Scenario 2 — One scheduler pass fires a due rule (US2 / SC-002)

Seed an enabled rule with `next_run_at` in the past whose scope resolves to ≥1 ACTIVE match, then
call `run_refresh_pass(session, now=now, batch_limit=100)`. Assert: exactly one `ScrapeJob`
(`type=SCHEDULED`, `source=SCHEDULER`) with one target per active match; its dispatch enqueued;
`last_run_at == run_time`; `next_run_at` moved one cadence into the future.

## Scenario 3 — Zero-match advance (US2 AS-4 / SC-006)

Due rule whose scope resolves to 0 active matches → no `ScrapeJob`, no dispatch, but `next_run_at`
advances (rule not perpetually re-selected).

## Scenario 4 — Concurrency, no duplicates (US3 / SC-003, live)

Two overlapping `run_refresh_pass` transactions over the same due set → each rule claimed and fired
by exactly one (SKIP LOCKED). Simulate crash: after claiming + enqueue, roll back instead of
commit → `next_run_at` unchanged → next pass re-fires exactly once; no double scheduled run.

## Scenario 5 — Workspace isolation (SC-004, live)

Rule in workspace A is invisible to workspace B via API even with an omitted app filter (RLS denies)
— cross-workspace read/write denial test.

## Scenario 6 — Backlog after downtime (FR-016 / SC-005)

Rule with `next_run_at` far in the past → single fire on the next pass; new `next_run_at` is the
next future occurrence, not one-per-missed-interval.

## Scenario 7 — Scope-target deletion (FR-020, live)

Delete a product/variant/group/competitor/match referenced by a rule → the delete succeeds and the
referencing rule is cascade-deleted; a subsequent pass neither blocks nor dereferences a missing
target.

## Scenario 8 — Import boundary stays green (FR-019)

`uv run pytest tests/unit/test_import_boundaries.py -q` still passes (croniter adds no
Scrapy/Twisted/Playwright/FastAPI import to `app_shared`). Optional new
`tests/unit/test_scheduler_import_boundary.py` asserts `apps/scheduler` imports nothing forbidden.
