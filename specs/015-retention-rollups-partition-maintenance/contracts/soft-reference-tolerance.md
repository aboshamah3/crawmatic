# Contract: Soft-Reference Tolerance (US4)

A correctness guard over existing read paths + a small operator-visibility check. **No schema change,
no new FK** (§22 forbids FKs into partitioned tables — they would block partition drop).

## The dangling reference
`match_current_prices.observation_id` — plain nullable `Uuid`, **no FK**, into `price_observations.id`
(`libs/shared/app_shared/models/observations.py:186`). After retention drops the referenced month's
partition, this id points at a row that no longer exists. This is the **only** soft reference into a
droppable partition (verified; other soft-ref UUIDs point among current-state tables, not into
partitioned raw tables).

## FR-021 — readers tolerate the absent row
Every reader of `match_current_prices` MUST:
- rely on the **denormalized fields** the row already carries (`price`, `old_price`, `currency`,
  `comparable`, `stock_status`, `success`, `error_code`, `extraction_method`,
  `extraction_confidence`, `scraped_at`) rather than joining to `price_observations`;
- if it *does* dereference `observation_id`, treat a miss as an expected `None` (no exception to the
  caller);
- never inner-join `match_current_prices → price_observations` on `observation_id` in a way that would
  filter out or error on a row whose observation is gone.

**Deliverable**: audit every existing consumer of `match_current_prices` (API read paths, price
comparison surfaces) and add/confirm a regression test that a `match_current_prices` row whose
`observation_id` points into a dropped partition still loads and returns correct denormalized data
(SC-007, US4 AS-1) with no 500/drop.

## FR-022 — dangling-soft-reference tolerance check (operator visibility)
A maintenance helper (`libs/shared/app_shared/maintenance/soft_refs.py`, invoked from the retention
task's report) that counts `match_current_prices.observation_id` values not resolvable to a live
`price_observations.id`, and classifies them as **expected / tolerated** (not corruption):
```
tolerated = count(match_current_prices WHERE observation_id IS NOT NULL
                  AND observation_id NOT IN (SELECT id FROM price_observations))
report.dangling_soft_refs_tolerated = tolerated   # informational only, never an error
```
This cross-tenant probe runs on the system session; it is best-effort and MUST never block or fail the
core create/rollup/drop guarantees (FR-024).

## Acceptance → requirement map
| US4 scenario | Mechanism |
|---|---|
| AS-1 reader loads current-state row with dangling ref | denormalized-field reads; no hard join (FR-021) |
| AS-2 explicit fetch of missing raw row → expected not-found; check reports tolerated | `None`-tolerant deref + tolerance check (FR-022) |
</content>
