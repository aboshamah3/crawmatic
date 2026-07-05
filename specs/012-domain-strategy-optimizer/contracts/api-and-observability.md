# Contract: Operator API + observability (FR-016, Constitution §24/§31)

## Operator API (`apps/api/app/routers/strategy.py`, versioned `/v1`, workspace-scoped)

All endpoints require the workspace dependency (Principle II) and use cursor-based pagination on lists
(default limit 50, max 500 — §24). Reuses the existing router/schema/service conventions.

| Method + path | Purpose | Notes |
|---------------|---------|-------|
| `POST /v1/strategy/discovery-runs` | Operator-trigger discovery for a `(competitor, domain, url_pattern)` with 3–10 `sample_urls` | Validates 3..10 → 422 on out-of-bounds (FR-019); enqueues `STRATEGY_DISCOVERY_RUN` (same task as auto — FR-016); returns the created run (`PENDING`) |
| `GET /v1/strategy/discovery-runs` | List discovery runs (cursor) | workspace-scoped |
| `GET /v1/strategy/discovery-runs/{id}` | Inspect one run | `scoped_get`; 404 cross-workspace |
| `GET /v1/strategy/profiles` | List learned profiles (cursor) | filterable by competitor/domain/status |
| `GET /v1/strategy/profiles/{id}` | Inspect one profile + its per-method stats | `scoped_get`; stats via `app_shared/strategy/repository.py` (joined to the scoped profile) |
| `PATCH /v1/strategy/profiles/{id}` | Operator override: set `url_pattern` override or `status = DISABLED`/re-enable | FR-006 / FR-014; guarded, workspace-scoped |

Response money/confidence fields serialize `Decimal` as strings (Constitution VII — no float). Error
responses use the structured `ScrapeErrorCode`/HTTP mapping already established.

## Observability (Constitution §31, emitted as structured JSON logs + counters)

| Event | When | Fields |
|-------|------|--------|
| `strategy_profile_seeded` | profile created `DISCOVERY_REQUIRED` (D5) or seeded by discovery | workspace, competitor, domain, url_pattern, source (AUTO/OPERATOR) |
| `strategy_method_promoted` | promotion applied (`contracts/promotion.md`) | profile_id, method_type, method_name, confidence, confirmed_success_count |
| `strategy_rediscovery_triggered` | rediscovery fires (`contracts/rediscovery.md`) | profile_id, reason, prior status → DEGRADED |
| `strategy_discovery_completed` | discovery run terminal | run_id, status (COMPLETED/NO_WINNER/FAILED), winning_* , sample_size |
| `strategy_stats_flushed` | flush task run | dirty_profiles, keys_flushed, rows_updated |
| `strategy_learned_start_used` | consumption resolver returns a start (SC-001) | profile_id, access_method, extraction_method |

These feed the §31 "strategy promotion/rediscovery events" observability requirement. No external
monitoring dependency (MVP). Counters (promotions, rediscoveries, learned-start hits, flush rows) are
incremented alongside the logs.
