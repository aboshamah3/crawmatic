# Crawmatic

Monorepo skeleton for the Crawmatic scraping platform: `api`, `scheduler`,
`worker`, `scrapers`, `scrapers-browser`, plus `postgres`, `pgbouncer`, and
`redis` infra, all brought up together with Docker Compose.

## Prerequisites

- Docker + Docker Compose v2.
- [`uv`](https://astral.sh/uv) 0.7.13+ (for local, non-container development
  and running tests): `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Bring-up (clean checkout)

1. Copy the example environment file — it ships with working local
   placeholders, no edits needed for a first bring-up:

   ```bash
   cp .env.example .env
   ```

   See `specs/001-monorepo-skeleton/contracts/environment.md` for the full
   variable catalogue. `.env` is git-ignored; `.env.example` is committed
   and holds no real secrets.

2. Build and start the whole stack with one command:

   ```bash
   docker compose up --build -d
   ```

   All eight components should reach running/healthy state with no further
   manual steps:

   ```bash
   docker compose ps
   # All services "running"; api "healthy" once its healthcheck passes.
   ```

## Validate the bring-up

### API health

```bash
curl -fsS http://localhost:${API_PORT:-8000}/health
# → {"status":"ok"}   (HTTP 200)
```

### Postgres reached only via PgBouncer

```bash
docker compose exec pgbouncer sh -c 'pgbouncer -V'   # image present & pinned
grep DATABASE_URL .env    # → host must be pgbouncer:6432, never postgres:5432
```

### Scrapyd nodes require auth and are internal-only

```bash
# Unauthenticated request is rejected (401):
docker compose exec worker sh -c 'curl -s -o /dev/null -w "%{http_code}" http://scrapers:6800/daemonstatus.json'
# → 401

# Authenticated request succeeds:
docker compose exec worker sh -c 'curl -s -u "$SCRAPYD_USERNAME:$SCRAPYD_PASSWORD" http://scrapers:6800/daemonstatus.json'
# → {"status": "ok", ...}

# Same two checks against scrapers-browser:6800.

# Scrapyd is NOT published to the host:
curl -sS --max-time 3 http://localhost:6800/ ; echo "  <- expected: connection refused (not published)"
```

### Tear down

```bash
docker compose down -v
```

For the full validation walkthrough (including dependency-boundary and
health-endpoint tests), see
`specs/001-monorepo-skeleton/quickstart.md`.

## Running tests

Install every workspace member before running anything — a plain `uv
sync` at the repo root leaves `apps/*`/`libs/*` uninstalled (this uv
workspace has no root package) and produces import/collection errors:

```bash
uv sync --locked --all-packages
```

Then, from the repo root:

```bash
# Migration-graph and workspace-scoping guards (fast, DB-independent):
bash scripts/check_single_head.sh
uv run python scripts/check_workspace_scoping.py

# Unit suite:
uv run pytest tests/unit -q

# Compose smoke (needs a reachable Docker daemon + Compose v2; skips
# cleanly otherwise) — brings up all 8 components and asserts they reach
# running/healthy:
cp .env.example .env   # required: docker-compose.yml's `env_file: .env`
                        # is a literal path, not resolved by --env-file
uv run pytest tests/integration/test_compose_smoke.py -v

# Live cross-workspace RLS isolation (needs a throwaway Postgres; the
# test applies the migrations and creates the two runtime roles itself):
cp .env.example .env   # required: the test imports app_shared.config,
                        # whose Settings fail fast on a missing variable —
                        # from a fresh checkout the run ERRORs at fixture
                        # setup without this, which reads as a broken test
                        # rather than a missing file
docker run -d --name cm-rls-test \
  -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \
  -e POSTGRES_DB=crawmatic -p 127.0.0.1:55444:5432 postgres:17.5-bookworm
RLS_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55444/crawmatic \
  uv run pytest tests/integration/test_rls_cross_workspace.py -v
```

`.github/workflows/ci.yml` runs exactly this sequence (as separate
`checks`/`rls`/`images`/`compose-smoke` jobs) on every push and pull
request.

## Provisioning the database roles (deploy step)

Workspace isolation is row-level security, and RLS is void for a
SUPERUSER and (without `FORCE`) for a table's owner — so the role that
serves requests must be neither. Roles are cluster-level objects with
passwords, which is why they are deliberately not in Alembic; they are
created by a one-shot step that runs **alongside** `alembic upgrade
head`, in the same migration image:

```bash
# once per environment, and safe to re-run on every deploy
docker compose run --rm migrate python -m migrate.provision_roles
```

It reads `MIGRATION_DATABASE_URL` (the owner/admin role) plus the
optional `CRAWMATIC_APP_DB_PASSWORD` / `CRAWMATIC_AUTH_DB_PASSWORD`,
executes `scripts/sql/rls_roles.sql` (the same statements `psql -f
scripts/rls_provision.sql` runs — one source of truth for the GRANTs),
and then **verifies the result**: it exits non-zero if `crawmatic_app`
ends up SUPERUSER or BYPASSRLS, owns any table, or if any RLS-enabled
table lost `FORCE`. See the posture block at the top of `.env.example`
for which URL gets which role.

Since the 2026-08-20 security review it also checks the isolation
posture from the `workspace_id` **column**: every relation in `public`
that carries one — monthly partitions included — must have RLS enabled,
`FORCE`d and policied. Asked the other way round ("of the tables where
RLS is on, which lack `FORCE`?"), the eight partitions of
`request_attempts` / `price_observations` / `price_alert_events` /
`webhook_events` that had no RLS at all were not candidates for the
check, and a direct `SELECT * FROM request_attempts_2026_08` returned
every workspace's rows to a tenant connection. Partitions now inherit
their parent's policies in three places that each have to hold: the
migration that closed the existing ones, the runtime partition-creation
job that makes next month's, and this deploy step.
