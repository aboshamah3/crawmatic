# Quickstart: Monorepo & Services Skeleton

Validation/run guide proving the skeleton boots and satisfies the spec's success criteria (SC-001…SC-006). Implementation details live in `tasks.md` and the implementation phase; this is a run/verify guide only.

## Prerequisites

- Docker + Docker Compose v2.
- `uv` (for local, non-container development): `curl -LsSf https://astral.sh/uv/install.sh | sh` (pin per `research.md` §Image Pinning — uv `0.7.13`).
- No cloud/platform account needed; everything runs locally on the same pinned images used in deployment.

## 1. Configure the environment

```bash
cp .env.example .env
# .env ships with working local placeholders; no edits needed for a first bring-up.
```

The committed `.env.example` enumerates every required variable (see `contracts/environment.md`). A new developer can bring the stack up without reading source code (SC-006).

## 2. Bring up the whole stack (one command)

```bash
docker compose up --build -d
```

This builds the five app images (`python:3.13.5-slim-bookworm` base, non-root) and starts all eight components: `api`, `scheduler`, `worker`, `scrapers`, `scrapers-browser`, `postgres`, `pgbouncer`, `redis`.

**Expected (SC-001):** every component reaches running/healthy with zero manual steps beyond this command.

```bash
docker compose ps
# All services "running"; api "healthy" once its healthcheck passes.
```

## 3. Validate the API health endpoint (SC-002, US1 AS2)

```bash
curl -fsS http://localhost:${API_PORT:-8000}/health
# → {"status":"ok"}   (HTTP 200)
```

## 4. Validate Postgres is reached only via PgBouncer (SC-003, US1 AS3)

```bash
# App services target pgbouncer:6432 — confirm the pooler is listening on 6432:
docker compose exec pgbouncer sh -c 'pgbouncer -V'   # image present & pinned
# DATABASE_URL host in .env must be pgbouncer:6432, never postgres:5432:
grep DATABASE_URL .env    # → ...@pgbouncer:6432/...
```

There is no schema in this phase, so no query is run; the check is that the routing (config + topology) points every app service at PgBouncer, never directly at Postgres (FR-011).

## 5. Validate Scrapyd nodes require auth and are internal-only (SC-004, SC-005, US2)

```bash
# From inside the internal network, unauthenticated request is rejected (401):
docker compose exec worker sh -c 'curl -s -o /dev/null -w "%{http_code}" http://scrapers:6800/daemonstatus.json'
# → 401

# Authenticated request succeeds:
docker compose exec worker sh -c 'curl -s -u "$SCRAPYD_USERNAME:$SCRAPYD_PASSWORD" http://scrapers:6800/daemonstatus.json'
# → {"status": "ok", ...}

# Same two checks against scrapers-browser:6800.

# Scrapyd is NOT published to the host (SC-005): this must fail/refuse:
curl -sS --max-time 3 http://localhost:6800/ ; echo "  <- expected: connection refused (not published)"
```

## 6. Validate dependency boundaries (FR-003)

```bash
uv run pytest tests/unit/test_import_boundaries.py -q
# Asserts app_shared cannot import scrapy/twisted/playwright, and app_shared
# does not depend on scrape_core.
```

## 7. Validate the health test (FR-005)

```bash
uv run pytest tests/integration/test_health.py -q
# GET /health → 200 {"status":"ok"}
```

## 8. Tear down

```bash
docker compose down -v
```

## Success-criteria mapping

| Check | Success criterion |
|-------|-------------------|
| Step 2 — all 8 up with one command | SC-001 |
| Step 3 — `/health` returns 200 | SC-002 |
| Step 4 — DB only via PgBouncer | SC-003 |
| Step 5 — Scrapyd auth + internal-only | SC-004, SC-005 |
| Steps 1–2 — clean checkout + `.env.example` only | SC-006 |

## Out of scope (do not expect here)

No database tables/migrations, no auth/API keys, no spiders that scrape, no rate limiting, no split Redis. Those arrive in SPEC-02+ (see `plan.md` Summary).
