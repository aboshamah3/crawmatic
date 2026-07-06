# Contract: Retention / Partition-Drop Job (US3)

**Task**: `MAINTENANCE_RETENTION_DROP = "maintenance.retention_drop"` (`maintenance` queue).
**Trigger**: scheduler enqueues every `RETENTION_INTERVAL_SECONDS`.
**Context**: BYPASSRLS system session (R9) — cross-tenant coverage/catalog reads + DDL.
**Home**: `apps/workers/app/workers/tasks_maintenance.py`; logic in
`libs/shared/app_shared/maintenance/partitions.py` + `.../retention.py`.

## Behavior

```
run_retention(session, *, now_utc) -> RunReport
  # Part A: partition-drop retention for registered partitioned tables
  for entry in PARTITIONED_TABLES:
    if to_regclass('public.'+entry.name) is None: report.tables_skipped_absent += entry.name; continue
    cutoff = now_utc - days(Settings[entry.retention_setting])
    for part in existing_partitions(entry.name):          # (name, [start,end)) from pg_catalog
      if not (part.end <= cutoff): continue               # FR-018: whole range strictly older
      if entry.feeds_rollups:                             # FR-016 (price_observations only)
        if not rollups_cover(session, part):              # date-level EXCEPT check (R7)
          report.partitions_skipped_pending_rollups += part.name; continue
      execute: DROP TABLE IF EXISTS {part.name};          # FR-015 partition-drop, never DELETE
      report.partitions_dropped += part.name
  # Part B: age-based row retention for the non-partitioned rollup table (R7)
  execute: DELETE FROM variant_price_daily_rollups
           WHERE date < (now_utc::date - RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS)
  return report
```

### `rollups_cover(session, part)` — verify-before-drop (FR-016, US3 AS-2, SC-004)
Date-level coverage over the partition's `[d0, dN)` (the clarified rule):
```
missing =  SELECT DISTINCT scraped_at::date FROM {part.name}          -- dates with source data
           EXCEPT
           SELECT DISTINCT date FROM variant_price_daily_rollups
             WHERE date >= d0 AND date < dN
return  missing is empty
```
Non-empty ⇒ retain + record `skipped_pending_rollups`; a later retention pass re-checks (self-healing;
edge case "rollups incomplete indefinitely" → surfaced for operator visibility, never silently
dropped). Cross-tenant, hence the system session.

## Rules
- **Partition-drop only** (FR-015, SC-003): raw partitions reclaimed by `DROP TABLE`, never bulk
  `DELETE`. The single sanctioned `DELETE` (Part B) is on the small, non-append-heavy, unpartitioned
  rollup table — not a raw partition (SC-003 is about raw partitions).
- **Deterministic cutoff** (FR-018, US3 AS-3, edge "retention window boundary"): eligible only when
  the entire half-open range is `<= cutoff`; a partition whose newest possible row is still in-window
  is left in place.
- **Per-table windows** (FR-017): each entry uses its own retention setting (obs/attempts 90,
  alerts 365, webhooks 90, rollups 730).
- **No-rollup tables drop by age alone** (FR-019, US3 AS-4): `feeds_rollups=False` skips the coverage
  check entirely.
- **Idempotent / concurrent-safe** (FR-020, edge "double run"): `DROP TABLE IF EXISTS`; an
  already-dropped partition is not an error and is never dropped twice.
- **Dangling soft refs** (edge case): dropping proceeds regardless of `match_current_prices.observation_id`
  pointing into the partition (no FK); readers tolerate it (see soft-reference-tolerance.md).
- **Vacuum/analyze** (FR-024): any optional housekeeping is best-effort and wrapped so it can never
  block/fail create/rollup/drop.

## Acceptance → requirement map
| US3 scenario | Mechanism |
|---|---|
| AS-1 expired + rollups complete → dropped | `end<=cutoff` ∧ `rollups_cover` → `DROP TABLE` |
| AS-2 expired + rollups incomplete → retained + skip recorded | `rollups_cover` false → skip |
| AS-3 newest row still in-window → left | `end<=cutoff` false |
| AS-4 no-rollup table → drop by age | `feeds_rollups=False` path (FR-019) |
</content>
