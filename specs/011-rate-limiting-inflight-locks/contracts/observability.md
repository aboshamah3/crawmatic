# Contract: Observability, Config & Error-Code Mapping

Covers FR-020, FR-021, FR-022; US4; SC-006. Constitution §31 (rate-limit hits, requeue/overflow
counts, dedup skips are named required metrics).

---

## Error-code mapping (reuse existing `ScrapeErrorCode` — no new codes)
| Event | Target `status` | `error_code` | Requirement |
|---|---|---|---|
| Rate-limit denial that ultimately **overflows** | `DEFERRED` | `RATE_LIMITED` | FR-020, US4 AS1 |
| Match lock already held | `SKIPPED` | `LOCKED_ALREADY_RUNNING` | FR-021, US4 AS2 |
| Normal fetch success/failure | `COMPLETED`/`FAILED` | (unchanged) | SPEC-08/10 |

Both codes flow through the **existing** `ScrapeResult → mark_target` path — no new persistence
writer. A transient rate-limit denial that later *succeeds* on a requeue records the normal
success outcome (only the terminal overflow carries `RATE_LIMITED`).

## Structured logs / counters (JSON — Constitution §31)
Emitted from the spider (limiter/lock outcomes) and pipeline (release). Keys namespaced per
`workspace_id` + `domain` + `access_method` where applicable:

| Signal | When | Fields |
|---|---|---|
| `rate_limit.hit` | token denied | workspace_id, domain, access_method, wait_hint |
| `rate_limit.requeue` | each in-spider backoff | workspace_id, match_id, requeue_count, delay |
| `rate_limit.overflow` | requeue cap exceeded → DEFERRED | workspace_id, scrape_job_id, match_id |
| `semaphore.denied` | concurrency slot denied | workspace_id, domain, access_method |
| `dedup.skip` | match lock already held | workspace_id, match_id (LOCKED_ALREADY_RUNNING) |
| `dedup.release` | lock released post-persist | workspace_id, match_id, released(bool) |

These are the §31 "per-domain rate-limit hits, requeue/overflow counts, dedup skips" the
observability principle names — no external monitoring dependency required for MVP.

## `Settings` knobs (`libs/shared/app_shared/config.py`) — all env-tunable (Principle IV)
| Setting | Default | Requirement |
|---|---|---|
| `RATE_LIMIT_DEFAULT_PER_MINUTE` | 60 | FR-008 fallback |
| `RATE_LIMIT_DEFAULT_CONCURRENCY` | 4 | FR-008 fallback |
| `RATE_LIMIT_KEY_TTL_SLACK_SECONDS` | 120 | FR-005 |
| `SEMAPHORE_SLOT_TTL_SECONDS` | 600 | FR-005, SC-004 |
| `MATCH_LOCK_HTTP_TTL_SECONDS` | 600 | FR-013 |
| `MATCH_LOCK_BROWSER_TTL_SECONDS` | 1800 | FR-013 |
| `REQUEUE_MAX_ATTEMPTS` | 5 | FR-017 |
| `REQUEUE_MAX_TOTAL_WAIT_SECONDS` | 300 | FR-017 |
| `RATE_LIMIT_JITTER_MIN_SECONDS` | 2 | FR-006 |
| `RATE_LIMIT_JITTER_MAX_SECONDS` | 20 | FR-006 |

**Invariant** (unit-tested): `min(MATCH_LOCK_HTTP_TTL_SECONDS, MATCH_LOCK_BROWSER_TTL_SECONDS)`
> `REQUEUE_MAX_ATTEMPTS × (typical wait_hint + RATE_LIMIT_JITTER_MAX_SECONDS)` so a working
owner never loses its lock (FR-013, US2 AS4).

## SC-006
Every overflow and every lock collision carries a structured code on the target/attempt — 100%
attributable — because both outcomes route through the single `mark_target` writer with the
code set, and are covered by an integration test asserting the persisted code.
