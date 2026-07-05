# Phase 1 Data Model: SPEC-13 Scheduler

One new persistent entity: **`refresh_rules`**. No changes to existing tables. Scrape jobs /
targets are reused as-is (SPEC-08). All patterns mirror existing workspace-owned models
(`Competitor`, `ScrapeJob`).

---

## Entity: RefreshRule → table `refresh_rules`

Module: `libs/shared/app_shared/models/refresh_rules.py`
Class: `class RefreshRule(Base, WorkspaceScopedBase, TimestampMixin)`

- `Base` → UUIDv7 `id` PK (`new_uuid7`), `NAMING_CONVENTION`.
- `WorkspaceScopedBase` → `workspace_id` (Uuid, not null, indexed).
- `TimestampMixin` → `created_at` / `updated_at` (`TZDateTime`, `_utc_now`, `onupdate`).

### Columns

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | `Uuid(as_uuid=True)` | no | `new_uuid7` | PK (from `Base`) |
| `workspace_id` | `Uuid(as_uuid=True)` | no | — | from `WorkspaceScopedBase`; indexed |
| `name` | `Text` | no | — | operator label |
| `scope` | `enum_column(ScrapeScope)` → `String(32)` | no | — | WORKSPACE/COMPETITOR/PRODUCT/VARIANT/PRODUCT_GROUP/MATCH (reuse `ScrapeScope`, research R6) |
| `product_id` | `Uuid(as_uuid=True)` | yes | NULL | set iff scope=PRODUCT |
| `product_variant_id` | `Uuid(as_uuid=True)` | yes | NULL | set iff scope=VARIANT |
| `product_group_id` | `Uuid(as_uuid=True)` | yes | NULL | set iff scope=PRODUCT_GROUP |
| `competitor_id` | `Uuid(as_uuid=True)` | yes | NULL | set iff scope=COMPETITOR |
| `match_id` | `Uuid(as_uuid=True)` | yes | NULL | set iff scope=MATCH |
| `cron_expression` | `Text` | yes | NULL | 5-field UTC cron; XOR with interval |
| `interval_minutes` | `Integer` | yes | NULL | minutes; XOR with cron; `> 0` |
| `priority` | `Integer` | no | `0` | advisory only (not claim-ordering) |
| `enabled` | `Boolean` | no | `true` | disabled rules never claimed |
| `next_run_at` | `TZDateTime` | yes | NULL | set on create; claim key |
| `last_run_at` | `TZDateTime` | yes | NULL | set on each fire |
| `locked_at` | `TZDateTime` | yes | NULL | last claim time (observability) |
| `created_at` | `TZDateTime` | no | `_utc_now` | from `TimestampMixin` |
| `updated_at` | `TZDateTime` | no | `_utc_now` / `onupdate` | from `TimestampMixin` |

All timestamps are TIMESTAMPTZ / tz-aware (FR-018). Enum stored as app-validated `VARCHAR(32)`
(no native PG enum), consistent with `ScrapeJob` (`enum_column`).

### Constraints (`__table_args__`, names per `NAMING_CONVENTION`)

1. `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_refresh_rules_workspace_id_workspaces")`.
2. Scope-target composite FKs (nullable, `ondelete="CASCADE"`, research R7) — use short constraint
   names to stay under Postgres' 63-byte cap:
   - `(workspace_id, product_id) -> products(workspace_id, id)` — `fk_rr_workspace_product_products`
   - `(workspace_id, product_variant_id) -> product_variants(workspace_id, id)` — `fk_rr_workspace_variant_variants`
   - `(workspace_id, product_group_id) -> product_groups(workspace_id, id)` — `fk_rr_workspace_group_groups`
   - `(workspace_id, competitor_id) -> competitors(workspace_id, id)` — `fk_rr_workspace_competitor_competitors`
   - `(workspace_id, match_id) -> competitor_product_matches(workspace_id, id)` — `fk_rr_workspace_match_matches`
3. `CheckConstraint("num_nonnulls(cron_expression, interval_minutes) = 1", name="exactly_one_cadence")`
   → `ck_refresh_rules_exactly_one_cadence` (FR-003).
4. `CheckConstraint("interval_minutes IS NULL OR interval_minutes > 0", name="interval_minutes_positive")`
   → `ck_refresh_rules_interval_minutes_positive`.
5. `CheckConstraint(<scope↔target-id matrix>, name="scope_target")` → `ck_refresh_rules_scope_target`
   (FR-002). Logic: for each scope, exactly its own target id is non-null and the other four are
   null; WORKSPACE ⇒ all five target ids null. Expressed as an `OR` of six per-scope clauses.

No `UniqueConstraint(workspace_id, id)` is added — nothing composite-FKs `refresh_rules`. Multiple
rules per (scope,target) are intentionally allowed (autospec-clarify: no uniqueness constraint).

### Indexes

- `ix_refresh_rules_workspace_id` (from `WorkspaceScopedBase`; also serves list keyset on
  `(created_at, id)` after the workspace filter).
- **Partial due-claim index**: `op.create_index("ix_refresh_rules_due", "refresh_rules",
  ["next_run_at"], postgresql_where=sa.text("enabled"))` — supports
  `WHERE enabled AND next_run_at <= now() ORDER BY next_run_at` (FR-007, Principle VIII).

### RLS (FR-005 — from the first migration)

In the creating migration, after table + index creation:
```python
for statement in emit_rls_policy("refresh_rules"):
    op.execute(statement)
```
Emits `ENABLE` + `FORCE ROW LEVEL SECURITY` + policy
`refresh_rules_workspace_isolation USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)`
(fail-closed to zero rows when the GUC is unset). The scheduler pass runs on the BYPASSRLS system
session (research R2) with app-level scoping preserved.

### State / lifecycle

`enabled` (true|false) is the only explicit state. Scheduling clock transitions per pass (within the
claiming transaction, research R3/R5):
```
claimed (FOR UPDATE SKIP LOCKED)
  → locked_at   = run_time
  → last_run_at = run_time
  → next_run_at = compute_next(cadence, run_time)   # strictly future (FR-013/016)
  → COMMIT
```
Rollback (crash) leaves all three unchanged; the row lock releases and a later pass re-claims
(FR-014).

---

## Registration checklist (so Alembic autogenerate & guards see it)

- `libs/shared/app_shared/models/__init__.py`: add
  `from app_shared.models.refresh_rules import RefreshRule` and `"RefreshRule"` to `__all__`.
- `libs/shared/app_shared/repository.py`: add `RefreshRule` to the `WORKSPACE_OWNED_MODELS`
  frozenset (required — it is tenant-owned; also read by `scripts/check_workspace_scoping.py`).
- Alembic `env.py` needs no change (imports `app_shared.models`; picks up via `__init__`).

## Migration

- New file under `/srv/crawmatic/crawmatic/alembic/versions/`, `down_revision = 'f30c60cfa2f7'`
  (current HEAD, SPEC-12 `domain_strategy_optimizer_tables`). Single linear history preserved.
- Hand-authored ops (no live Postgres in build env), mirroring the SPEC-08 jobs migration:
  `sa.Uuid(as_uuid=True)`, enums as `sa.String(length=32)`, timestamps as
  `sa.DateTime(timezone=True)`, explicit `PrimaryKeyConstraint`/`ForeignKeyConstraint`/
  `CheckConstraint`, then the partial index and `emit_rls_policy` loop.
- `downgrade()`: `op.drop_index("ix_refresh_rules_due", ...)` then `op.drop_table("refresh_rules")`
  (policy/RLS dropped with the table).

## Reused entities (unchanged)

- **ScrapeJob / ScrapeJobTarget** (`app_shared/models/jobs.py`) — created by a fired rule via
  `create_scope_job(..., job_type=SCHEDULED, source=SCHEDULER, scope=<rule scope>)`.
  `ScrapeJobType.SCHEDULED` and `ScrapeJobSource.SCHEDULER` already exist as forward-compat enum
  members — no model/enum change.
- **CompetitorProductMatch** (`app_shared/models/competitors_matches.py`) — read-only during
  scope→active-match resolution (`status == MatchStatus.ACTIVE`).
- **ProductGroupItem** (`app_shared/models/catalog.py`) — read-only for PRODUCT_GROUP resolution
  (`product_id` XOR `product_variant_id` membership).
