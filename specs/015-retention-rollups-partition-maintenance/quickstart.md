# Quickstart & Validation: Retention, Rollups & Partition Maintenance

**Feature**: SPEC-15 | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

Validation guide only — implementation lives in `tasks.md`/implementation. Details are in
[data-model.md](./data-model.md) and [contracts/](./contracts/). This build env has **no live
Postgres/Docker daemon** (see user memory), so live-DB checks are `*_live.py` tests that `skipif`-probe
and skip cleanly; offline migration render + pure-unit tests run everywhere.

## Prerequisites
- `uv sync --all-packages` (workspace deps).
- Env: `SYSTEM_DATABASE_URL` (or `AUTH_DATABASE_URL` fallback) → a BYPASSRLS role; `MIGRATION_DATABASE_URL`
  → direct Postgres (not the pooler) for alembic.

## Setup / build checks
```bash
# New rollups migration renders offline and keeps a SINGLE head (no live DB needed)
uv run alembic upgrade head --sql | grep -E "CREATE TABLE variant_price_daily_rollups"
uv run alembic heads                 # exactly one "(head)"
uv run pytest tests/unit/test_migration_offline_rollups.py -q

# Import-boundary: no Scrapy/Twisted/Playwright in the new maintenance code (Principle I/V, FR-003)
uv run pytest tests/unit/test_import_boundaries.py -q
# Workspace-scoping guard passes (sanctioned unscoped scans annotated # noqa: workspace-scope)
uv run python scripts/check_workspace_scoping.py
```

## US1 — Next month's partitions exist before writes need them
```bash
uv run pytest tests/unit/test_partition_bounds.py -q          # Dec→Jan, Feb, half-open bounds (FR-007)
uv run pytest tests/integration/test_partition_create_live.py -q
```
Expect: current + next-month partitions exist for every **existing** registered table; re-run is a
no-op (FR-006); a missing current-month partition is self-healed (FR-005); `webhook_events` (absent)
is skipped without error (FR-002); a write dated into next month succeeds.

## US2 — Daily rollups summarize each day's pricing
```bash
uv run pytest tests/unit/test_rollup_aggregation.py -q        # currency filter, min/avg/max, count, Decimal (FR-011/012/013)
uv run pytest tests/integration/test_daily_rollup_live.py -q
```
Expect: exactly one `variant_price_daily_rollups` row per (workspace, variant, day) with correct
`client_price` + competitor min/avg/max + `comparable_competitor_count` + `latest_alert_type`;
currency-mismatched competitor prices excluded from aggregates and count (SC-006); a variant with zero
comparable competitors still gets a row (count 0, competitor prices NULL); re-running the same day
upserts (no duplicate/corruption, SC-002).

## US3 — Expired partitions dropped only after rollups verified
```bash
uv run pytest tests/unit/test_retention_eligibility.py -q     # cutoff = whole range < now-window (FR-018)
uv run pytest tests/integration/test_retention_drop_live.py -q
```
Expect: an expired `price_observations` partition with complete date-coverage in
`variant_price_daily_rollups` is dropped via `DROP TABLE` (not `DELETE`, SC-003); one with missing
coverage is **retained** and reported `skipped_pending_rollups` (SC-004); a partition still in-window
is untouched; `request_attempts`/`price_alert_events` drop by age alone with their own windows
(FR-019); re-run does not drop twice (FR-020).

## US4 — Readers tolerate references into dropped partitions
```bash
uv run pytest tests/integration/test_soft_ref_tolerance_live.py -q
```
Expect: a `match_current_prices` row whose `observation_id` points into an already-dropped partition
still loads and returns correct denormalized data with no error/500/row-drop (SC-007); the
dangling-soft-reference tolerance check reports such refs as tolerated, not corruption (FR-022).

## Success-criteria coverage
| SC | Where validated |
|---|---|
| SC-001 no write fails for missing partition | US1 create-ahead (daily cadence, weeks of lead) |
| SC-002 idempotent re-run | US1/US2/US3 re-run tests |
| SC-003 100% drop / 0% raw DELETE | US3 `DROP TABLE` assertion, no bulk DELETE on raw tables |
| SC-004 no drop before rollups verified | US3 verify-before-drop test |
| SC-005 bounded retention per window | US3 per-table window tests |
| SC-006 one row/(ws,variant,day), currency-correct | US2 aggregation tests |
| SC-007 dangling-ref reads succeed | US4 tolerance test |
</content>
