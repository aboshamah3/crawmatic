# Quickstart: Validating the Database Foundation

How to prove SPEC-02 works. Split into **(A) DB-independent** checks that run in any environment (this build env included) and **(B) live-Postgres** checks that run on a host with a running Postgres (no Docker daemon here).

Prerequisites: uv workspace synced (`uv sync`). New deps this spec adds: `alembic`, `uuid6` (in `app_shared`).

---

## A. DB-independent validation (run here)

Run the unit suite:

```bash
uv run pytest tests/unit -q
```

Expected coverage / outcomes:

1. **Naming convention disambiguates** (FR-002, SC-003) — `tests/unit/test_naming_convention.py`: a table with `uq(group_key, code_a)` and `uq(group_key, code_b)` yields two **distinct** names (`uq_..._group_key_code_a`, `uq_..._group_key_code_b`). See [contracts/models-base.md](./contracts/models-base.md).
2. **UUIDv7** (FR-003) — `tests/unit/test_ids.py`: `new_uuid7()` is a stdlib `UUID`, `.version == 7`, sequential calls are time-ordered. See [contracts/ids.md](./contracts/ids.md).
3. **Money boundary** (FR-005, SC-004) — `tests/unit/test_money.py`: `float`, `NaN`, `Infinity`, and over-scale (`Decimal("1.23456")`) are rejected; `Decimal("19.99")` / `Decimal("0.0001")` round-trip exactly as `Decimal`. See [contracts/money.md](./contracts/money.md).
4. **Naive-timestamp guard** (FR-004) — `tests/unit/test_base_model.py`: assigning a naive `datetime` to a `TZDateTime` column raises `ValueError`; the demo model exposes a UUIDv7 pk and tz-aware `created_at`/`updated_at`.
5. **RLS DDL fail-closed** (FR-007) — `tests/unit/test_rls_policy.py`: `emit_rls_policy("t")` renders `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and the fail-closed `NULLIF(current_setting('app.workspace_id', true), '')::uuid` predicate (empty context → zero rows, never an error). See [contracts/rls.md](./contracts/rls.md).
6. **Import boundary intact** (Principle I/V) — `tests/unit/test_import_boundaries.py`: the new `app_shared` submodules (`models`, `ids`, `money`, `enums`) import no scrapy/twisted/playwright and no `scrape_core`.
7. **No eager engine** (FR-008, SC-005) — importing `app_shared.database` creates no engine (existing test still passes).

Offline migration render (no DB):

```bash
uv run alembic upgrade head --sql
```

Expected: SQL text containing `CREATE TABLE`, `TIMESTAMPTZ`, `NUMERIC(18, 4)`, `uuid`, and the two distinct unique names — proving the first migration is well-formed and `TIMESTAMPTZ`/`NUMERIC(18,4)` render correctly (asserted by `tests/unit/test_migration_offline.py`).

Single-head guard (CI):

```bash
bash scripts/check_single_head.sh    # exits 0 iff `alembic heads` reports exactly one head
```

Expected: exit 0 (one head). This is the CI check that fails on multiple heads (FR-012, SC-006).

---

## B. Live-Postgres validation (run on a Postgres-capable host)

These require a running Postgres and Docker daemon — **not available in this build env**; authored and marked (`@pytest.mark.<live_db>` / skipped without `MIGRATION_DATABASE_URL`).

Set the direct URL (distinct from the pooler `DATABASE_URL`):

```bash
export MIGRATION_DATABASE_URL='postgresql+psycopg://crawmatic:crawmatic@localhost:5432/crawmatic'
```

1. **Run the one-shot migration job** (SC-001, US1):
   ```bash
   uv run alembic upgrade head
   ```
   Expected: upgrades to head; the demonstration table exists. Via compose, the one-shot `migrate` service runs the same command against `postgres:5432` directly (not `pgbouncer`).

2. **Connectivity check** (FR-015, SC-007):
   ```bash
   uv run pytest tests/integration/test_db_connectivity.py
   ```
   Expected: `check_connection()` opens a session and executes `SELECT 1`.

3. **Migration through compose** (US1, AS-1/2/3):
   ```bash
   docker compose run --rm migrate     # one-shot; connects directly to postgres:5432
   ```
   Expected: exits 0 after `alembic upgrade head`; no app service (`api`/`scheduler`/`worker`) runs migrations at startup.

4. **RLS fail-closed against a real table** (optional, marked): apply `emit_rls_policy()` to a throwaway table; with no `SET LOCAL app.workspace_id`, a select returns **zero** rows; with `SET LOCAL app.workspace_id = '<uuid>'`, only matching rows return.

---

## Traceability

Every check above maps to a spec requirement/success criterion (FR-001…FR-015, SC-001…SC-007) and a contract in [contracts/](./contracts/). Implementation details (exact test bodies, migration code, Dockerfile) belong to `tasks.md` and the implementation phase — not this guide.
