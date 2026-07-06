# Quickstart / Validation Guide: Webhook Events

Validates SPEC-16 end-to-end. Prereqs, then unit checks (run anywhere) and integration checks
(skip cleanly without a live Postgres/Redis, per the existing skipif DB probe — no container engine
in this build env).

## Prerequisites

```bash
cd /srv/crawmatic/crawmatic
uv sync --all-packages          # plain `uv sync` wipes workspace member deps — always --all-packages
```

Live integration also needs Postgres (via PgBouncer), Redis, and Alembic migrated to head; those
are deferred here and the tests skip when unavailable.

## 1. Migration is a single linear head

```bash
uv run alembic heads            # must print exactly ONE head = the new webhook migration
uv run pytest tests/unit/test_strategy_single_head.py -q
uv run pytest tests/unit/test_migration_offline_webhooks.py -q   # NEW: asserts CREATE TABLE + RLS trio + partitions
```
Expected: one head; the new migration's `down_revision == '4a1dca402f78'`; offline render shows
`webhook_endpoints` (plain) and `webhook_events` (`PARTITION BY RANGE (created_at)`, composite PK
`(id, created_at)`), current+next month partitions, and `emit_rls_policy` output for both tables.

## 2. Registry / retention untouched (webhook_events already registered)

```bash
uv run pytest tests/unit/test_partition_registry.py tests/unit/test_retention_eligibility.py -q
```
Expected: still exactly 4 `PARTITIONED_TABLES`, `webhook_events → RETENTION_WEBHOOK_EVENTS_DAYS (90)`.
No registry entry was added. (SC-006: retention runs through the existing maintenance job, no new
scheduler.)

## 3. Scopes already present (no enum edit)

```bash
uv run pytest tests/unit/test_scopes.py -q
```
Expected: `Scope.WEBHOOKS_READ == "webhooks:read"`, `Scope.WEBHOOKS_WRITE == "webhooks:write"` exist;
no duplicates added.

## 4. Event taxonomy + payload builders (pure unit)

```bash
uv run pytest tests/unit/test_webhook_payloads.py -q
```
Expected:
- Each `AlertEventType`/`ScrapeJobStatus`/`StrategyStatus` trigger maps to the correct
  `WebhookEventType` string (see contracts/events.md).
- `AlertEventType.UNCHANGED` and `ScrapeJobStatus.CANCELLED` produce **no** event.
- Every built payload is JSON-serializable and under the size guard (< 8 KiB); an over-size payload
  raises.

## 5. Secret never leaks (pure unit)

```bash
uv run pytest tests/unit/test_webhook_response_guard.py -q
```
Expected: `WebhookEndpointResponse` has no `secret`/`secret_encrypted`/`secret_key_version` field;
`has_secret: bool` is present.

## 6. Endpoint CRUD + SSRF + isolation (integration; skips w/o DB)

```bash
uv run pytest tests/integration/test_api_webhook_endpoints.py -q
```
Covers (US2):
- Create with a public `https://` URL round-trips; `has_secret` reflects whether a secret was sent;
  raw secret never in any response, but a row in the DB has non-null `secret_encrypted`.
- Create/update with each of: private/loopback/link-local/metadata IP, internal hostname,
  `user:pass@host` userinfo, non-http(s) scheme → `422 UNSAFE_URL`, nothing persisted (SC-002).
- Update name/enabled/event_types/url/secret persists; `updated_at` advances; `secret: null` clears.
- Delete → 404 on subsequent get, absent from list.
- Another workspace cannot see/update/delete (404 / absent); no-context session → 0 rows (SC-004).
- `webhooks:read` alone cannot create/update/delete (403); write needs `webhooks:write`.

## 7. Poll API + pagination + isolation (integration; skips w/o DB)

```bash
uv run pytest tests/integration/test_api_webhook_events.py -q
```
Covers (US1):
- Seed N events (spanning ≥ 2 monthly partitions), list with page size P < N, walk `next_cursor` to
  exhaustion → every event exactly once, deterministic `(created_at, id)` order, no dup/gap (SC-001).
- Filter by `event_type` returns only that type, still paginated.
- `GET /v1/webhook-events/{id}` returns the full event; `delivered_at` is null (SC-007).
- Empty workspace → `{items: [], next_cursor: null}`; past-the-end cursor → same; malformed cursor →
  `422 INVALID_CURSOR`.
- Cross-workspace fetch → 404; cross-workspace list → absent; no-context → 0 rows (SC-004).

## 8. Automatic event creation on domain changes (integration; skips w/o DB + broker)

```bash
uv run pytest tests/integration/test_api_webhook_events.py -q -k "seam or created"
```
Covers (US3, SC-003, SC-005): triggering each source (alert transition via `recompute_variant`, job
finalize via `finalize_jobs`, strategy promotion/rediscovery via `flush_stats`) yields exactly one
event of the expected type in the correct workspace; forcing the enqueue to raise does not fail or
roll back the source operation (event creation is post-commit and try/except-guarded).

## Success-criteria trace

| SC | Validated by |
|---|---|
| SC-001 poll exactly-once across ≥2 partitions | §7 |
| SC-002 SSRF reject all unsafe / accept all valid | §6 |
| SC-003 one event per source change, right workspace | §8 |
| SC-004 zero cross-workspace / zero no-context rows | §6, §7 |
| SC-005 event creation never blocks/corrupts source | §8 |
| SC-006 90-day partition-drop via existing job, no new scheduler | §2 |
| SC-007 no delivery: delivered_at null, no outbound HTTP | §7, §4 |
