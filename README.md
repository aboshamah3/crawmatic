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
