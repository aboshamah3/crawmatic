# Quickstart / Validation Guide: Scrape Profiles & Extraction Rules

How to validate SPEC-06. Everything DB/Redis-independent runs **here** (no Docker daemon required); DB/Redis-dependent items are authored and skipped without a live Postgres+Redis host. See `contracts/` and `data-model.md` for the interface details this guide references.

## Prerequisites

- uv workspace synced: `uv sync`.
- No live Postgres/Redis needed for the unit suite. Live items require `DATABASE_URL`/`MIGRATION_DATABASE_URL` (Postgres 17 via PgBouncer for the app, direct for migrations) and `REDIS_URL`.

## 1. Unit validation (runs in this environment)

```bash
uv run pytest tests/unit -q
```

Proves, with **no** DB/Redis:

- **Model** (`test_scrape_profiles_models.py`): `scrape_profiles` column set/types/nullability; **nullable** `workspace_id` + FK; both partial unique indexes with exact predicates; documented defaults; enums; all names ≤63 bytes.
- **RLS render** (`test_rls_scrape_profiles.py`): `emit_global_readable_rls_policy` → ENABLE/FORCE + `FOR SELECT` (own|global) + `FOR ALL` write (own-only, `WITH CHECK`).
- **Validators** (`test_profile_validation.py`): enum accept/reject; regex compile-ok / un-compilable-reject / catastrophic-reject; cookie technical-accept / session-auth-reject; `validation_rules` currency + money (finite/scale/non-neg) + min≤max + text-lists; `confidence_rules` `[0,1]`. `test_money.py`: `parse_money` boundary.
- **Confidence defaults** (`test_confidence_defaults.py`): §17 values; `resolve_confidence_rules` merge/override.
- **Resolution** (`test_profile_resolution.py`, `test_profile_resolution_cache_key.py`): chain ordering across all precedence combos; visibility fall-through; domain-strategy no-op; `NONE_RESOLVED`; grouping one-result-per-group; match-override precedence; deterministic cache key.
- **Repository** (`test_profiles_repository.py`): `visible_profiles_select` = own∨global; `owned_profile_select` = own-only; `assert_profile_assignable` accepts own/global/null, rejects dangling/cross-ws.
- **Bulk upsert** (`test_profiles_upsert.py`): one `ON CONFLICT (workspace_id, name) WHERE workspace_id IS NOT NULL DO UPDATE`; last-wins dedup; valid/rejected split.
- **API wiring** (`test_scrape_profiles_scope_gating.py`, `test_scrape_profiles_routes_registered.py`, `test_scopes.py`): scopes correct per method; router mounted; `scrape_profiles:read/write` in the vocabulary.
- **Migration offline** (`test_migration_offline_scrape_profiles.py`): `alembic upgrade head --sql` renders the table + partial uniques + custom RLS + the three `ON DELETE SET NULL` FKs; single head.
- **Import boundary** (`test_import_boundaries.py`): `app_shared.models.scrape_profiles` + `app_shared.profiles.*` import no fastapi/scrapy/twisted/playwright.

CI guards:

```bash
uv run python scripts/check_workspace_scoping.py   # still green (ScrapeProfile intentionally out of the guarded set — research D2)
bash scripts/check_single_head.sh                  # one Alembic head
```

## 2. Live validation (Postgres + Redis host — authored, skipped here)

```bash
alembic upgrade head          # creates scrape_profiles + custom RLS + promotes the 3 assignment FKs
uv run pytest tests/integration -q
```

Covers (`tests/integration/test_scrape_profiles_*` / `test_profile_*`):

- **CRUD round-trip**: create with `validation_rules`/`confidence_rules` bundles → read back byte-identical; unique name per workspace; invalid payloads (enum/regex/cookie/rules) → `422` (SC-001/SC-006).
- **Isolation** (SC-007): cross-workspace profile invisible to writes; a global (`workspace_id IS NULL`) profile readable by every workspace; the tenant path **cannot** create/edit/delete a global row (RLS write policy blocks it); no-context → zero own rows (globals still visible).
- **Assignment** (SC-002): assign a profile as competitor default, match override, and workspace default — accepted for own+global, rejected cross-workspace (`422`), cleared with null; deleting a referenced profile nulls the references (`ON DELETE SET NULL`, FR-023).
- **Bulk upsert** (SC-008): mixed valid/invalid → all valid upserted, every invalid reported in `rejected[]`, batch never aborted; re-push updates in place; bounded statement count.
- **Resolution** (SC-003/SC-004/SC-005): precedence across match→competitor→workspace→global returns exactly the dictated profile; `NONE_RESOLVED` when nothing supplies one; a batch of ≥10k matches over a few `(competitor_id, url_pattern)` groups performs lookups proportional to groups (not matches); a second resolution within the TTL is a Redis cache hit; a relevant profile/assignment write is reflected within the TTL (or immediately if invalidated).

## 3. Global-default seeding (out-of-band, research D11)

The terminal `global_default` profile (and any other `workspace_id IS NULL` global) is **not** seeded by the migration or the tenant API — the RLS write policy blocks tenant global writes. Create globals via a privileged/`BYPASSRLS` platform connection (the migration/seed role), e.g. an ops script inserting a `scrape_profiles` row with `workspace_id = NULL, name = 'global_default'`. Absent a global default, resolution returns `NONE_RESOLVED` (a valid terminal state, FR-016).

## Done when

- `tests/unit` green here; both CI guards green.
- Live suite authored and marked (skips cleanly without Postgres/Redis).
- All eight SC-00x acceptance criteria have a corresponding unit and/or live test.
