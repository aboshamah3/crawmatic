# Secrets by component (EPA A3/F03)

Which environment variable **names** each deployed service may hold.
Values are never recorded here or anywhere in this repository — only
names, and the file/secret-store location they come from. This is the
operational half of `scripts/sql/grants_expected.yaml`'s DB-role split:
a role with narrow table privileges is undermined if the *credential*
that would let a process authenticate as a broader role still sits in
its container's environment.

This is a **target state**. Applying it (rotating the scrapyd services'
`DATABASE_URL` from `crawmatic_app` to the new `crawmatic_scraper` role,
and removing every variable a service does not need) is owner work
tracked as plan task **A10** — nothing in this document changes a
Railway variable by itself. `scripts/provision_db_roles.py
--check-deployment-config` already asserts one slice of this (the
`api` service never holds `SYSTEM_DATABASE_URL`); the table below is the
full inventory that check is one instance of.

## Services

| Service | DB role it authenticates as | DB URL variable(s) |
|---|---|---|
| `api` | `crawmatic_app` (ordinary), `crawmatic_auth` only via `AUTH_DATABASE_URL` (pre-auth lookup — see below) | `DATABASE_URL`, `AUTH_DATABASE_URL` |
| `worker` | `crawmatic_app` (ordinary), `crawmatic_auth` via `SYSTEM_DATABASE_URL` (maintenance/reconcile tasks) | `DATABASE_URL`, `SYSTEM_DATABASE_URL` |
| `scheduler` | `crawmatic_app` (ordinary), `crawmatic_auth` via `SYSTEM_DATABASE_URL` (due-rule cross-tenant scan) | `DATABASE_URL`, `SYSTEM_DATABASE_URL` |
| `migrate` | `crawmatic_migrate` | `MIGRATION_DATABASE_URL` |
| `scrapers` | **today:** `crawmatic_app` (no dedicated role existed). **target (A10):** `crawmatic_scraper` | `DATABASE_URL` |
| `scrapers-browser` | **today:** `crawmatic_app`. **target (A10):** `crawmatic_scraper` | `DATABASE_URL` |

## Variable NAMES each service may hold, by component

### `api`

- `DATABASE_URL` — `crawmatic_app`, workspace-scoped.
- `AUTH_DATABASE_URL` — `crawmatic_auth`, BYPASSRLS. **Not** a finding
  per `provision_db_roles.py`'s `check_deployment_config`: the pre-auth
  credential lookup (`app_shared.database.get_auth_session`, used for
  login-by-email and API-key-prefix lookup) is structurally cross-tenant
  — it runs *before* any `app.workspace_id` exists to scope it with.
- `REDIS_URL`
- Service-token / webhook-signing secrets the API's own routers need
  (outbox dispatch, webhook signature verification).
- **Forbidden**: `SYSTEM_DATABASE_URL`. Nothing under `apps/api` calls
  `get_system_session`; its presence would be pure standing privilege
  with no caller — exactly what `check_deployment_config` fails on.

### `worker`

- `DATABASE_URL` — `crawmatic_app`.
- `SYSTEM_DATABASE_URL` (falling back to `AUTH_DATABASE_URL`, config.py)
  — `crawmatic_auth`, for the maintenance sweep, netledger
  open/close/reconcile, and lease-sweeper tasks that must run on the
  sanctioned cross-tenant BYPASSRLS seam (`get_system_session`).
- `REDIS_URL`
- Proxy provider API credentials the reconciliation import script needs
  (`scripts/import_dataimpulse_usage.py` reads these, not the worker
  process itself, but they are provisioned into the same environment).

### `scheduler`

- `DATABASE_URL` — `crawmatic_app`.
- `SYSTEM_DATABASE_URL` — `crawmatic_auth`, for the due-rule claim scan
  (`app_shared.scheduling`), which is inherently cross-tenant.
- `REDIS_URL`

### `migrate`

- `MIGRATION_DATABASE_URL` — `crawmatic_migrate`. Nothing else: this
  one-shot job runs `alembic upgrade head` and
  `scripts/provision_db_roles.py --provision`, then exits. It must
  never hold `SYSTEM_DATABASE_URL`/`AUTH_DATABASE_URL`/any runtime
  service credential — `crawmatic_migrate` cannot read tenant rows at
  all (NOBYPASSRLS, and FORCE ROW LEVEL SECURITY applies even to the
  table owner), so holding a BYPASSRLS DSN alongside it would be
  privilege the job has no use for and no business holding.
- The three role-password GUCs consumed by `provision_db_roles.sql`
  (`CRAWMATIC_APP_DB_PASSWORD`, `CRAWMATIC_AUTH_DB_PASSWORD`,
  `CRAWMATIC_SCRAPER_DB_PASSWORD`, `CRAWMATIC_MIGRATE_DB_PASSWORD`) —
  these are the ONLY place any runtime role's password may be read from
  an environment; `provision_db_roles.sql`'s password contract (see its
  header) keeps each one transaction-local.

### `scrapers` / `scrapers-browser` (target state, A10)

The scrapyd HTTP and browser nodes. **Narrowest footprint of any
service** — a compromised spider (arbitrary page content, a malicious
redirect chain, a supply-chain issue in a scraping dependency) should
never be able to reach anything beyond what one scrape run legitimately
touches.

- `DATABASE_URL` — pointed at **`crawmatic_scraper`**, not
  `crawmatic_app`. Its privilege set (`scripts/sql/grants_expected.yaml`)
  is exactly: `INSERT` on `request_attempts`/`price_observations`/
  `network_operations`; `UPDATE` on `scrape_job_targets`; `SELECT` on
  `domain_strategy_profiles`/`scrape_profiles`/
  `competitor_product_matches`. Nothing else — no budgets, no
  entitlements, no `users`, no credential tables.
- `REDIS_URL` — job/result plumbing with `scrapyd` (queueing, item
  pipelines' Redis-backed dedupe where used).
- `SCRAPYD_USERNAME` / `SCRAPYD_PASSWORD` — Scrapyd's own HTTP basic
  auth (already deployed; unrelated to the database role split).
- Proxy provider credentials (`encrypt_proxy_password.py`'s target;
  read from `proxy_providers.password_encrypted` at runtime, decrypted
  with a key the scraper process holds) and any egress-guard-relevant
  configuration (`scrape_core.browser.egress_guard`).
- `NETLEDGER_BUFFER_PATH` — the local durable buffer path
  `app_shared.netledger.buffer.DurableEventBuffer` writes to when a
  ledger close cannot reach the database immediately.
- **Forbidden**: `SYSTEM_DATABASE_URL`, `AUTH_DATABASE_URL`,
  `MIGRATION_DATABASE_URL`, any admin/service-token variable the API
  uses, any owner/admin credential. None of the scraping pipeline's
  code paths call `get_system_session`/`get_auth_session`, and holding
  one of these variables would be exactly the kind of standing
  privilege with no legitimate caller that `check_deployment_config`
  exists to catch for the `api` service today — the same rule should
  extend to the scraper services once A10 cuts them over.

## Applying this (A10, owner work — not done by this task)

1. Provision `crawmatic_scraper`'s password via
   `CRAWMATIC_SCRAPER_DB_PASSWORD` and run
   `scripts/provision_db_roles.py --provision` (idempotent; also
   re-applies every other role's current attributes/grants).
2. Build a `crawmatic_scraper`-authenticated `DATABASE_URL` and set it
   on the `scrapers` and `scrapers-browser` Railway services, replacing
   their current `crawmatic_app`-authenticated one.
3. Remove every variable from those two services' environments that is
   not listed above for them (owner/admin service tokens, any
   `SYSTEM_DATABASE_URL`/`AUTH_DATABASE_URL` that may have been copied
   in from another service's variable set at some point).
4. Verify with `scripts/verify_grants.py` (confirms the role's actual
   grants match the manifest) and a canary scrape run (confirms the
   pipeline still writes `request_attempts`/`price_observations`/
   `network_operations` and updates `scrape_job_targets` successfully
   under the new, narrower role).
5. Only after step 4 passes clean: consider whether `crawmatic_app`'s
   privileges on the fleet-ingestion tables it no longer needs to write
   (see `scripts/sql/grants_expected.yaml`'s comments on
   `network_operation_settlements`/`provider_usage_records`) can be
   narrowed further now that the scraper role is the one actually
   writing them in production.

## Known gaps not yet closed by A3 (tracked, not silently dropped)

Two of the eight tables/roles this task's own code audit checked could
**not** be narrowed to the plan's literal target without breaking a
live code path that is out of this task's file scope to migrate:

- `crawmatic_app` keeps `SELECT` on `network_operations` because
  `apps/api/app/services/admin_usage.py` joins it directly for the
  admin usage report.
- `crawmatic_auth` keeps `SELECT`+`INSERT`+`UPDATE` on
  `network_operations` (unchanged from before this task) because
  `app_shared.netledger.recorder`'s open/close/replay path
  (`get_system_session`) issues real `SELECT`s against it as part of
  the ledger's idempotent-close guarantee.

Fully closing either requires migrating that consumer onto
`workspace_usage_v` (the tenant-safe view this task adds) first. See the
A3 task report's Escalation section for the specific follow-up.
