# Contract: authenticated, idempotent Scrapyd dispatch (`app_shared.scrapyd.client`)

`ScrapydDispatchClient` — plain `requests` (no scrapy/twisted), consumed by a thin `apps/workers` Celery task (FR-018/019, §4/§8, US4, research D8).

## `schedule(project, spider, *, workspace_id, scrape_job_id, match_ids, mode, batch_index) -> jobid`

- POSTs Scrapyd `schedule.json` on a `Settings.SCRAPYD_HTTP_URLS` node with HTTP **basic auth** (`SCRAPYD_USERNAME` / `SCRAPYD_PASSWORD`).
- Passes the spider args (`workspace_id`, `scrape_job_id`, `match_ids`, `mode`) through **unchanged** (US4 scenario 3).
- Returns the Scrapyd `jobid` on success.

## Authentication (Principle VI)

- The Scrapyd node **requires** basic auth (already wired in `apps/scrapers/scrapyd.conf` from `SCRAPYD_USERNAME`/`SCRAPYD_PASSWORD`). An unauthenticated `addversion.json`-capable node is RCE.
- Missing/incorrect credentials → Scrapyd responds `401`; the client raises and **no** run is scheduled (US4 scenario 2 / SC-005).

## Idempotency (§8, FR-019)

- `dispatch_key(scrape_job_id, batch_index) -> f"dispatched:{scrape_job_id}:{batch_index}"`.
- Guard with Redis `SET NX` (via `app_shared.redis_client`) **before** the network call; if the key already exists the schedule is a **no-op** (returns the persisted jobid) — a retried at-least-once Celery dispatch never double-runs the batch.
- The returned Scrapyd `jobid` is persisted as the durable backstop.

## Tests (unit, fake Scrapyd + fake Redis)

- Correct creds + args → `schedule.json` called with basic auth, args intact → jobid.
- Missing/wrong creds → `401` → raises, no schedule.
- `SET NX` guard: a second dispatch of the same `(scrape_job_id, batch_index)` is a no-op (no second POST).
