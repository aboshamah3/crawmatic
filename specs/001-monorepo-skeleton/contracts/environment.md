# Contract: Environment Configuration (`.env.example`)

Every service loads its configuration from the environment at startup (FR-017); the repo commits an `.env.example` enumerating all variables with placeholder (non-secret) values so a new developer can populate real values without guessing (SC-006). Variables are drawn from master doc §6.

## Variable catalogue

### Database (api, scheduler, worker; scrapers later)

| Variable | Example / placeholder | Notes |
|----------|----------------------|-------|
| `DATABASE_URL` | `postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic` | Host is **pgbouncer:6432**, never postgres:5432 (FR-011). |
| `DB_POOL_SIZE` | `5` | Per-process SQLAlchemy pool cap (§4 connection budget). |
| `DB_MAX_OVERFLOW` | `2` | Per-process overflow cap. |

### Redis (worker; others as needed)

| Variable | Example | Notes |
|----------|---------|-------|
| `REDIS_URL` | `redis://redis:6379/0` | Single instance in skeleton; split broker/locks deferred. |

### Scrapyd pools & auth (worker; scrapers nodes)

| Variable | Example | Notes |
|----------|---------|-------|
| `SCRAPYD_HTTP_URLS` | `http://scrapers:6800` | **Comma-separated pool** (FR-018). |
| `SCRAPYD_BROWSER_URLS` | `http://scrapers-browser:6800` | Comma-separated pool. |
| `SCRAPYD_USERNAME` | `scrapyd` | Basic-auth user for both nodes (FR-012). |
| `SCRAPYD_PASSWORD` | `change-me` | Placeholder; real secret injected per environment. |

### API surface

| Variable | Example | Notes |
|----------|---------|-------|
| `API_PORT` | `8000` | **Canonical** API port — the only API-port variable a developer sets. It is simultaneously the host-published port and (via compose `environment: PORT=${API_PORT}`) the container's uvicorn `--port $PORT`, so listen/publish/healthcheck ports never diverge (FR-017; public-exposure boundary is FR-013). The container-internal `PORT` is derived, not separately configured. |
| `API_PUBLIC_BASE_URL` | `https://api.example.com` | External base URL. |
| `INTERNAL_API_BASE_URL` | `http://api:8000` | Internal base URL. |

### Local infra bootstrap (compose only)

| Variable | Example | Notes |
|----------|---------|-------|
| `POSTGRES_USER` | `crawmatic` | Postgres superuser for local stack. |
| `POSTGRES_PASSWORD` | `crawmatic` | Local placeholder. |
| `POSTGRES_DB` | `crawmatic` | Local DB name. |
| `PGBOUNCER_AUTH_TYPE` | `trust` | **Local only**; deployed envs MUST use `scram-sha-256` with a real userlist (spec Assumptions, §4). |
| `PGBOUNCER_POOL_MODE` | `transaction` | Transaction pooling (§4). |
| `PGBOUNCER_LISTEN_ADDR` | `*` | Dual-stack (IPv4+IPv6) bind for PgBouncer; maps to the pgbouncer image's `LISTEN_ADDR` (FR-016). |

## Rules

- **Fail fast on missing required vars** (FR-017, Edge Cases): a service with an absent required variable exits with a clear error instead of starting half-configured. `app_shared/config.py` (pydantic-settings) centralizes this.
- **No real secrets committed**: `.env.example` holds placeholders; real `.env` is git-ignored.
- **Same images across environments**: only the environment differs between local compose and deployed platform (spec Assumptions); no code changes to switch environments.
- **Comma-separated Scrapyd URLs are pools** even when length 1 (FR-018).
