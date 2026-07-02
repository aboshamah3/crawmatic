# Contract: Service Topology, Exposure & Start Commands

Defines the eight-component topology, each component's network exposure, bind behaviour, and start command. Derived from master doc §4, §6 and spec FR-006…FR-016.

## Components (8)

| # | Component | Image / build | Start command | Host-published? | Internal port | Dual-stack bind |
|---|-----------|---------------|---------------|-----------------|---------------|-----------------|
| 1 | `api` | build (`python:3.13.5-slim-bookworm`) | `uvicorn app.main:app --host :: --port $PORT` | **Yes** — `${API_PORT}` only | `${API_PORT}` (e.g. 8000) | Yes (`::`) |
| 2 | `scheduler` | build | `python -m app.scheduler.scheduler_app` | No | — | n/a |
| 3 | `worker` | build | `celery -A app.workers.celery_app worker --loglevel=info` | No | — | n/a |
| 4 | `scrapers` (Scrapyd HTTP) | build (project baked) | `scrapyd` | No | 6800 | Yes |
| 5 | `scrapers-browser` (Scrapyd browser) | build (project + Playwright Chromium baked) | `scrapyd` | No | 6800 | Yes |
| 6 | `postgres` | `postgres:17.5-bookworm` | image default | No | 5432 | n/a |
| 7 | `pgbouncer` | `edoburu/pgbouncer:v1.23.1-p3` | image default (`transaction` mode) | No | 6432 | Yes (`listen_addr = *`) |
| 8 | `redis` | `redis:7.4.2-bookworm` | image default | No | 6379 | n/a |

## Exposure rules (FR-013, SC-005)

- **Only `api` publishes a host port.** All other components use compose `expose:` (internal network only), never `ports:`.
- Scrapyd nodes MUST NOT be reachable publicly (their `addversion.json` is RCE-adjacent, §4).

## Connectivity (§6)

```text
api        → pgbouncer:6432 → postgres:5432
scheduler  → pgbouncer:6432 → postgres:5432
worker     → pgbouncer:6432 → postgres:5432
worker     → scrapers:6800 (pool)          [wiring only; no calls in skeleton]
worker     → scrapers-browser:6800 (pool)  [wiring only; no calls in skeleton]
scrapers*  → pgbouncer:6432 → postgres:5432 [later specs]
<members that need it> → redis:6379
```

- **No component connects to `postgres` directly** (FR-011). The one-shot migration job that connects directly is OUT OF SCOPE (SPEC-02).
- `SCRAPYD_HTTP_URLS` / `SCRAPYD_BROWSER_URLS` are **comma-separated pools**, treated as pools even with one entry (FR-018).

## Scrapyd auth contract (FR-012, SC-004)

- Both Scrapyd nodes enable HTTP basic auth (`username`/`password` from `SCRAPYD_USERNAME` / `SCRAPYD_PASSWORD`).
- An authenticated internal request to `GET http://scrapers:6800/` (or `daemonstatus.json`) succeeds.
- A request without valid credentials is rejected (401).

## Boot ordering & resilience (Edge Cases)

- App services that will use the DB depend on `pgbouncer` (compose `depends_on`), which depends on `postgres`.
- If Postgres is not yet accepting connections at startup, DB-using services wait/retry the pooler rather than crash-loop unmanaged; the skeleton's `GET /health` is dependency-free so the API becomes healthy regardless.

## Non-root (FR-015)

- Every built image (`api`, `scheduler`, `worker`, `scrapers`, `scrapers-browser`) runs as an unprivileged user via `USER`. Infra images run as their non-root defaults.

## Image pinning (FR-014)

- Every image above is pinned to a concrete tag; no `latest`. (Digest pinning is a documented follow-up.)
