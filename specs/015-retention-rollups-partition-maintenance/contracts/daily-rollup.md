# Contract: Daily Rollup Job (US2)

**Task**: `MAINTENANCE_DAILY_ROLLUP = "maintenance.daily_rollup"` (`maintenance` queue).
**Trigger**: scheduler enqueues every `DAILY_ROLLUP_INTERVAL_SECONDS`; also invokable for an explicit
backfill day.
**Context**: BYPASSRLS system session (R9) — cross-tenant source scan; **app-level workspace scoping
preserved** (explicit `workspace_id=` on every aggregate filter and every upsert, per FR-014).
**Home**: `apps/workers/app/workers/tasks_maintenance.py`; aggregation SQL in
`libs/shared/app_shared/maintenance/rollups.py`.

## Signature
```
run_daily_rollup(session, *, target_date=None) -> RunReport
  # target_date defaults to the most recent COMPLETED UTC day (yesterday UTC). (clarification)
```

## Behavior (for UTC date D = target_date)

Driven by `price_observations` with `scraped_at::date = D` (R6 — the clarified rollup-date source):

```
1. Scan (cross-tenant, # noqa: workspace-scope) the distinct
   (workspace_id, product_variant_id, product_id) that have >=1 observation on D.
2. Per (workspace_id, product_variant_id):
   a. competitor aggregate over price_observations WHERE scraped_at::date = D
        AND workspace_id = :ws AND product_variant_id = :pv
        AND success AND comparable AND price IS NOT NULL AND currency = :client_currency
      -> min(price)  -> cheapest_competitor_price
         avg(price)  -> average_competitor_price   (exact NUMERIC, FR-012)
         max(price)  -> highest_competitor_price
         count(*)    -> comparable_competitor_count   (0 allowed, FR-013)
   b. client_price, currency, latest_alert_type  <- variant_price_states (scoped by ws+pv)  # R6
   c. UPSERT variant_price_daily_rollups
        (workspace_id, product_id, product_variant_id, date=D, currency, client_price,
         cheapest/average/highest_competitor_price, comparable_competitor_count, latest_alert_type)
        ON CONFLICT (workspace_id, product_variant_id, date) DO UPDATE  (FR-010)
   report.rollups_upserted += 1
3. commit; return report
```

- **Currency filter** (FR-011, US2 AS-4): `currency = :client_currency` in the competitor aggregate
  excludes mismatched-currency prices from min/avg/max **and** the count. `comparable` is the persisted
  SPEC-09 flag (already false on currency mismatch) — read, not recomputed.
- **Zero comparable competitors** (FR-013, US2 AS-3): a variant with observations but no comparable
  same-currency prices → row with `client_price`, NULL competitor min/avg/max, count 0.
- **No spurious rows** (edge case): a variant with no observations on D gets no row; a day with no
  observations at all yields no rollups and does not block unrelated retention.
- **Idempotent** (FR-010, US2 AS-2, SC-002): upsert on the unique key — re-run/backfill of D updates
  in place, never duplicates or corrupts.
- **Money**: exact `Decimal`/`NUMERIC`, finite-only (FR-012); no float, no NaN/Inf.
- **Isolation** (FR-014): rollup rows are workspace-owned; every read/write carries `workspace_id`;
  RLS is the defense-in-depth layer the system session bypasses only for the cross-tenant scan.

## Acceptance → requirement map
| US2 scenario | Mechanism |
|---|---|
| AS-1 one correct row per (ws, variant, day) | driver scan + upsert (FR-009/010) |
| AS-2 re-run updates in place | `ON CONFLICT … DO UPDATE` (FR-010) |
| AS-3 zero competitors → client price kept, agg empty, count 0 | LEFT-agg semantics (FR-013) |
| AS-4 currency mismatch excluded | `currency = :client_currency` filter (FR-011) |

## Ordering / coverage linkage
Running the rollup for D creates exactly the rows the retention coverage check (retention-drop.md)
looks for on date D — so once every activity-date in a partition's range has been rolled up, that
partition is droppable (SC-004).
</content>
