# Contract: `generic_price_spider` arguments & lifecycle

**Module**: `apps/scrapers/price_monitor/spiders/generic_price_spider.py` (FR-001).

## Arguments (Scrapyd `schedule.json` → spider kwargs)

| Arg | Type | Required | Meaning |
|-----|------|----------|---------|
| `workspace_id` | UUID string | yes | scopes **every** DB query (FR-002) |
| `scrape_job_id` | UUID string | yes | correlation id stored on observations/attempts (nullable column) |
| `match_ids` | comma-separated UUIDs (or JSON list) | yes | the targets to scrape |
| `mode` | string | no | reserved/pass-through in this slice — only `HTTP` (⇒ `DIRECT_HTTP`) is honored; other transport modes (proxy/browser) and their enumerated values are defined by the later access-policy/browser specs. Absent ⇒ `HTTP`. |

## Lifecycle (per FR-002/003/004/007/020, §8 steps 1–11)

1. Parse + validate args; refuse to start on a missing `workspace_id`/`match_ids`.
2. Open a workspace-scoped read (via the D1 seam, `set_workspace_context`) and load the matches with `scoped_select(CompetitorProductMatch, workspace_id).where(id IN match_ids)` — a match not in the workspace is simply absent (no cross-read).
3. For each match, obtain its **cached resolved** scrape profile (SPEC-06 resolution cache — consume, do not re-walk the chain per match) and the competitor `robots_policy`.
4. Yield one `DIRECT_HTTP` request per match URL (no proxies/browser/rate-limiter/dedup in this slice).
5. In `parse`: run extraction (contracts/extraction.md) → validation (contracts/price-validation.md); build a `ScrapeResult` item (success or failure) carrying the observation + attempt fields; `yield` it to the batched pipeline.
6. On fetch failure (timeout / DNS / 4xx-5xx / SSRF rejection): yield a `success=false` `ScrapeResult` with the mapped error code (contracts/errors.md) — still exactly one request attempt.

## Boundary (FR-020, Principle V)

The spider **stops at persistence**. It MUST NOT compute alerts, variant price states, alert events, or webhooks, and MUST NOT emit a `price_analysis` task. (Those are later specs.) It also does not update `scrape_job_targets` in this slice (that table is out of scope — see plan.md Complexity Tracking / research D9); it records each match's terminal outcome via the observation/attempt `success` fields.

## Workspace isolation (FR-002, Principle II)

Every read/write is workspace-scoped (`scoped_select`/`scoped_get` + `set_workspace_context` so RLS is active). No row from another workspace is ever read or written. Cross-workspace + no-context (fail-closed, 0 rows) covered by live isolation tests.
