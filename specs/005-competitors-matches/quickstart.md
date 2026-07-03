# Quickstart / Validation: Competitors & Matches

How to validate SPEC-05. DB-independent logic is fully unit-tested **in this environment**; items needing a live Postgres are authored and **skipped** until run on a PG-capable host (no Docker daemon / live Postgres here). Details live in `contracts/` and `data-model.md` — this is the run guide.

## Prerequisites
- uv workspace synced: `uv sync`.
- No new third-party dependencies (uses existing SQLAlchemy pg dialect, FastAPI/Pydantic, stdlib `urllib.parse`/`ipaddress`/`re`).
- Live-DB validation additionally needs a reachable Postgres 17 (`MIGRATION_DATABASE_URL`) with the `crawmatic_app` (no BYPASSRLS) + `crawmatic_auth` (BYPASSRLS) roles from SPEC-03, plus the SPEC-04 head (`c2987b29555e`) already applied.

## 1. Unit tests (run here — no DB)
```bash
uv run pytest tests/unit -q
```
Expected new/extended coverage (all pass without a DB):
- `test_competitors_matches_models.py` — §22 shapes, the two unique keys, composite workspace-local FKs, **explicit `cpm` constraint names all ≤63 bytes**, health defaults, `Integer`/`Numeric(5,4)` types, `VARCHAR(32)` enum columns, no naive timestamps.
- `test_rls_competitors_matches.py` — `emit_rls_policy` renders ENABLE+FORCE+fail-closed policy for **both** tables.
- `test_url_safety.py` — the accept/deny corpus: public http(s) + public IP literals accepted; localhost / private (10/8, 172.16/12, 192.168/16) / loopback / link-local (169.254/16, fe80::/10) / unique-local (fc00::/7) / metadata (169.254.169.254) / internal hostname+suffix / userinfo / non-http(s) scheme rejected, each with the mapped `UnsafeUrlReason` (IPv4 + IPv6).
- `test_url_pattern.py` — `normalize_url` (host lowercased, `www.`/default-port/fragment/trailing-slash stripped, **query kept**) + `derive_url_pattern` (`:id` id-like segments, `*` product slugs after known keys incl. locale-prefixed, locale preserved, scheme+query dropped) + `URL_PATTERN_ALGORITHM_VERSION` stamped.
- `test_matches_upsert.py` — `build_matches_upsert` compiles to a **single** `ON CONFLICT (4 cols) DO UPDATE SET ...` (incl. `updated_at=now()`, **excluding** health cols); `prepare_match_urls` safe/unsafe split; `dedup_last_wins` on the conflict key; `resolve_match_variants` fills `product_id` from the parent.
- `test_matches_scope_gating.py` / `test_competitors` scope gating — routers declare correct `require_scopes` (read vs write; bulk-upsert = write).
- `test_competitors_matches_scoping_guard.py` — CI guard flags a planted `select(Competitor)` / `select(CompetitorProductMatch)` without `workspace_id`; clean tree passes.
- `test_migration_offline_competitors_matches.py` — `alembic upgrade head --sql` renders both tables + unique keys + RLS (both); single head.
- `test_import_boundaries.py` (extended) — `app_shared.models.competitors_matches` / `app_shared.url_safety` / `app_shared.url_pattern` / `app_shared.matches.*` import **no** fastapi/scrapy/twisted/playwright.
- `test_workspace_consistency.py` (extended) — match-shaped competitor/variant refs accepted in-workspace, rejected cross/absent.

## 2. CI guards (run here)
```bash
uv run python scripts/check_workspace_scoping.py   # OK — 2 new models now guarded, no unscoped access
bash scripts/check_single_head.sh                  # single head after the competitors/matches revision
```

## 3. Migration offline render (run here)
```bash
SPECIFY_FEATURE_DIRECTORY=specs/005-competitors-matches uv run alembic upgrade head --sql | less
# Expect: CREATE TABLE competitors | competitor_product_matches,
#         the two competitor uniques + the 4-col match unique + 3 composite FKs,
#         ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY + CREATE POLICY (x2 tables).
```

## 4. Live-DB validation (run on a Postgres host — skipped here)
```bash
uv run alembic upgrade head        # creates the 2 tables + RLS online
uv run pytest tests/integration -q # live-marked scenarios
```
Scenarios (map to spec Acceptance / Success Criteria):
- **US1 / SC-001** — `POST /v1/competitors` stores name/domain with `legal_status=REVIEW_REQUIRED`/`robots_policy=RESPECT`/`status=ACTIVE`; a second create with the same domain → `409 DUPLICATE_DOMAIN`; read/update/list/delete round-trip in the workspace; delete returns `{outcome:"hard_deleted"}`.
- **US2 / SC-002/003/004/005** — `POST /v1/matches` with a valid public product URL → stored with `normalized_competitor_url` + `url_pattern` + `url_pattern_version`; a localhost/private/metadata/credentialed/non-http(s) URL → `422 UNSAFE_URL`, not stored; the same variant matched to many competitors/URLs → all stored; an exact `(variant, competitor, normalized URL)` duplicate → `409 DUPLICATE_MATCH`.
- **US3 / SC-006** — bulk-upsert a batch → all safe created (each with normalized URL + pattern + version); re-push with changes → matched rows updated in place (by the 4-col key), **0** duplicates; a batch mixing safe + unsafe URLs → unsafe in `rejected[]`, safe still upserted; a large batch runs in a **bounded** number of statements (one `ON CONFLICT` for all safe rows — assert via query log, no per-row loop); health fields unchanged by a re-push.
- **US4 / SC-007/008** — workspace-A caller sees **0** of workspace-B's competitors/matches (by id and in lists); a query with the app filter omitted still returns 0 rows (RLS); no workspace context → 0 rows (fail-closed); a match referencing another workspace's variant/product/competitor → `422 WORKSPACE_MISMATCH`, 0 stored; a `matches:read`-only key cannot write; a `matches:write` key can; the scoping guard fails CI on any introduced unscoped competitor/match query.

## Rollback
```bash
uv run alembic downgrade -1   # drops competitor_product_matches then competitors; returns to head c2987b29555e
```
