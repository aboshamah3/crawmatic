# Contract: Partition Creation Job (US1)

**Task**: `MAINTENANCE_PARTITION_CREATE = "maintenance.partition_create"` (`maintenance` queue).
**Trigger**: scheduler loop enqueues every `PARTITION_CREATE_INTERVAL_SECONDS` (default daily —
weeks of lead so next-month always exists before the month begins, SC-001).
**Context**: BYPASSRLS system session (`get_system_session`) — DDL + catalog reads, no workspace rows.
**Home**: `apps/workers/app/workers/tasks_maintenance.py`; logic in
`libs/shared/app_shared/maintenance/partitions.py` (scraping-free).

## Behavior

```
create_missing_partitions(session, *, now_utc, lookahead_months) -> RunReport
  for entry in PARTITIONED_TABLES:                       # registry (data-model §2)
    if to_regclass('public.'+entry.name) is None:        # FR-002 / R4
      report.tables_skipped_absent += entry.name; continue
    for (suffix, start, end) in months(now_utc, 0 .. lookahead_months):   # current + next (FR-004/005)
      if partition {entry.name}_{suffix} already exists (catalog check):  # FR-006 idempotent
        continue
      execute: CREATE TABLE IF NOT EXISTS {entry.name}_{suffix}
               PARTITION OF {entry.name} FOR VALUES FROM ('{start}') TO ('{end}');
      report.partitions_created += "{entry.name}_{suffix}"
  return report
```

- **Bounds** (`months`): mirror migration `_month_partition_bounds` — half-open
  `[YYYY-MM-01, <next-month>-01)`, UTC, correct across Dec→Jan and Feb lengths (FR-007, US1 edge case).
- **Current-month self-heal**: offset range starts at `0`, so a missing current-month partition is
  also created (FR-005, US1 AS-3).
- **Idempotent** (FR-006, US1 AS-2): existence pre-check + `IF NOT EXISTS`; re-run creates/alters
  nothing, raises nothing, reports zero changes.
- **RLS**: inherited from the parent's policy — **no** per-partition RLS DDL (research R2).
- **PK/partition-key**: unchanged — parents already declare composite PK incl. the partition column;
  this job creates children only, never alters parents (FR-008).

## Acceptance → requirement map
| US1 scenario | Mechanism |
|---|---|
| AS-1 next-month created with correct bounds | `months(now,1)` + `PARTITION OF … FROM/TO` |
| AS-2 re-run is a no-op | catalog pre-check + `IF NOT EXISTS` (FR-006) |
| AS-3 missing current month created | offset starts at 0 (FR-005) |
| AS-4 registered-but-absent table skipped | `to_regclass` NULL → skip (FR-002) |

## Concurrency
Two overlapping runs: `CREATE TABLE IF NOT EXISTS` + Postgres DDL locking make duplicate creation a
no-op; no partition created twice (spec edge case "double run"). No global lock needed.
</content>
