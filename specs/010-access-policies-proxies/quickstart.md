# Quickstart & Validation: Access Policies, Proxies & Request Attempts

Runnable validation for SPEC-10. Follows the SPEC-05..09 convention: pure engines are
exhaustively unit-tested with **no infra**; DB/Redis/Celery/spider/API paths use integration
tests that **skip cleanly** when Postgres/Redis are absent (this build environment has the
`uv`/pytest toolchain but no live stack). Design details live in `contracts/` and
`data-model.md` — this file is the run/verify guide.

## Prerequisites

- `uv sync --all-packages` (workspace install — plain `uv sync` wipes member deps).
- Env: existing `DATABASE_URL`/`MIGRATION_DATABASE_URL`/`REDIS_URL`, plus the new
  `ENCRYPTION_KEYS="1:<urlsafe-b64-fernet-key>"` and `ENCRYPTION_PRIMARY_KEY_VERSION=1`
  (generate a key with `python -c "from cryptography.fernet import Fernet;
  print(Fernet.generate_key().decode())"`).

## 1. Migration (single head)

```bash
uv run alembic heads          # expect exactly one head = the new revision
uv run alembic upgrade head   # (live Postgres only) creates the 3 tables + RLS
```

Offline/no-DB check: `uv run alembic upgrade head --sql` renders DDL containing
`proxy_providers`/`access_policies`/`domain_access_rules`, the dual read/write RLS policies
for the two dual-scope tables, the single isolation policy for `domain_access_rules`, and
both partial-unique namespaces. `request_attempts` is **not** in the diff (already exists).

## 2. Pure-engine unit tests (no infra — must run everywhere)

```bash
uv run pytest tests/unit/test_access_engine.py \
              tests/unit/test_policy_resolution.py \
              tests/unit/test_encryption.py -q
```

Expected: the full `next_attempt` strategy × attempt-number × flag matrix
(`access-engine.md`); `DIRECT_ONLY` never proxies; `max_retries=0` → one plan then STOP;
`proxy_budget_exhausted` reroutes/stops per strategy; resolution precedence
(domain_rule > workspace > global), disabled-rule fall-through, URL-pattern-beats-domain-only;
encryption round-trip + unknown-key-version raises + rotation re-encrypt.

## 3. CRUD API (skip-clean integration)

```bash
uv run pytest tests/integration/test_api_access.py -q
```

Validates (Scenarios US1-1..5): policy round-trips every field; proxy `password` accepted,
stored as ciphertext, response exposes only `has_password` (SC-003); cross-workspace
read/write denied and globals read-only (SC-005); no-context → zero tenant rows; unsafe
`base_url` → 422 `UNSAFE_URL`.

Manual smoke (live API):

```bash
# create a proxy provider (password never returned)
curl -sX POST $API/v1/proxy-providers -H "Authorization: Bearer $KEY" \
  -d '{"name":"dc-us","type":"DATACENTER","base_url":"http://proxy.example.com:8000",
       "username":"u","password":"s3cret","country_code":"US","monthly_budget_limit":100000}'
# -> 201, body has "has_password": true and NO password field

# create an access policy: direct first, proxy on retry
curl -sX POST $API/v1/access-policies -H "Authorization: Bearer $KEY" \
  -d '{"name":"default","strategy":"DIRECT_THEN_PROXY","max_retries":2,
       "use_proxy_on_retry":true,"max_requests_per_minute":60,"timeout_ms":30000}'

# override one domain
curl -sX POST $API/v1/domain-access-rules -H "Authorization: Bearer $KEY" \
  -d '{"competitor_id":"<uuid>","domain":"shop.example.com","access_policy_id":"<policy>",
       "max_concurrent_requests":2,"max_requests_per_minute":30,"cooldown_seconds":5}'
```

## 4. Budget & ceilings (fake-redis unit + skip-clean integration)

```bash
uv run pytest tests/unit/test_access_budget.py tests/integration/test_access_budget_redis.py -q
```

Validates: monthly `INCR` flips `allowed=False` past `monthly_budget_limit`; a new `%Y_%m`
resets; per-minute/hour/day ceilings independent; per-domain cooldown gate; **no**
`request_attempts` query anywhere in the module (FR-010, SC — assert by import/AST or a grep
guard).

## 5. Spider integration — access strategy + attempt logging (skip-clean)

```bash
uv run pytest tests/integration/test_spider_access.py -q
```

Validates (Scenarios US2-1/2, US3-1..3, SC-001/002/006):
- `DIRECT_THEN_PROXY` + failed direct → a second attempt via `PROXY_HTTP`; two
  `RequestAttempt` rows (`attempt_number` 1 & 2), the retry carrying provider/country.
- `DIRECT_ONLY` → no attempt ever proxied.
- Disabled/missing provider degrades (fallback or `PROXY_FAILED`), never crashes.
- Exactly one `RequestAttempt` per attempt; writes batched & off the reactor thread (reuses
  the SPEC-07/08 `BatchedPersistencePipeline`); each row lands in the correct monthly
  partition with `access_method`/proxy fields set.
- Decrypted proxy password never appears in captured logs.

## 6. Full guard sweep

```bash
uv run pytest -q                      # whole suite (integration auto-skips w/o infra)
uv run python scripts/check_workspace_scoping.py   # DomainAccessRule scoped; dual-scope tables use access.repository
```

## Success mapping

| Success criterion | Where proven |
|-------------------|--------------|
| SC-001 configured direct/proxy sequence 100% | §2 engine matrix + §5 spider integration |
| SC-002 exactly N attempt rows for N attempts | §5 (one `RequestAttempt` per attempt) |
| SC-003 no plaintext credentials (rest or API) | §2 encryption + §3 response redaction |
| SC-004 domain rule overrides default when enabled | §2 resolution precedence |
| SC-005 workspace isolation / zero no-context rows | §1 RLS + §3 cross-workspace tests |
| SC-006 millions/month via partitioning, off-reactor batched | §1 (existing partition) + §5 pipeline |
