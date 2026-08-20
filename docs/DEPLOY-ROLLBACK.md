# Deploy order and rollback

What "deploy" and "roll back" mean for this repo's five Railway services
(`migrate`, `api`, `scheduler`, `worker`, `scrapers` + `scrapers-browser`) —
each deployed independently, unlike the SaaS repo's single `deploy.sh`. This
document does not itself run anything; it is the reference the operator reads
before touching Railway.

## Deploy order: migrate → api → workers/scheduler → scrapers

```
migrate  ─▶  api  ─▶  scheduler + worker  ─▶  scrapers + scrapers-browser
(schema)     (serves)  (enqueue/consume)      (widest fan-out)
```

Each stage exists to protect the next one from a state it isn't ready for:

1. **`migrate` first, always, and it must finish before anything else
   starts.** `docker-compose.yml`'s own comment on the `migrate` service says
   it plainly: it is a one-shot Alembic job, gated behind the `migrate`
   profile so a normal bring-up doesn't start it by accident, and "no app
   service (api/scheduler/worker) runs migrations at startup; they only
   depend on pgbouncer." That line is the whole reason this repo is
   different from the SaaS repo, where the server Dockerfile runs `prisma
   migrate deploy` on container start and migration-before-serving is
   therefore automatic. Here it is not automatic — nothing else in this
   stack applies a migration, so if `migrate` is skipped or only
   partially completes, every other service starts against whatever schema
   happened to be there before.

2. **`api` next.** The API is what customers and the SaaS frontend actually
   call, and its routers query tables the migration step may have just
   added, renamed, or added constraints to. Deploying `api` before `migrate`
   has finished means it serves traffic against a schema its own code
   assumes exists — the RLS migration below is the sharpest version of this
   failure mode, but any added-column/added-table migration has the same
   shape: `api` code written against the new schema, running against the
   old one, produces errors instead of features. Deploying `api` right after
   `migrate` (rather than waiting for the async services too) keeps the
   window in which customers see a stale `api` as short as possible.

3. **`scheduler` and `worker` next, together.** These are the services that
   *create* work — `scheduler` enqueues jobs on a cadence
   (`SCHEDULER_POLL_INTERVAL_SECONDS`), `worker` consumes them via Celery.
   Both must not run old code that enqueues or processes against a schema
   the migration has already moved past — an old worker reading a column
   that a migration renamed fails the job, and depending on the failure
   mode may fail it in a way that looks like a scraping problem rather than
   a deploy-ordering one. They come after `api` rather than before it
   because they are asynchronous background processors: a customer notices
   a broken `api` response within seconds, but a stale scheduler/worker
   pair degrades more gradually (jobs queue up, or fail quietly, before
   anyone external notices), so `api` correctness is the tighter deadline.

4. **`scrapers` and `scrapers-browser` last.** These are the widest
   fan-out — many scrape targets, many domains, the components actually
   reaching out to the internet — and the least coupled to the database
   schema directly (they report results back through `worker`, they don't
   query Postgres themselves the way `api` does). Being both the widest
   blast radius and the most loosely coupled to the migration is exactly
   why they deploy last: there is no schema-correctness reason to rush
   them, and rushing them buys nothing but a bigger simultaneous change if
   something goes wrong.

## Rollback: reverse order, and it's an image swap, not a schema swap

Rolling back means redeploying each service's previous image, **in the
reverse of the deploy order**: scrapers/scrapers-browser first, then
worker/scheduler, then api, then — if the migration itself needs undoing —
migrate last, and only if the decision below says so.

The reverse order exists for the same reason the forward order does, run
backwards: undo the widest-fan-out, least-coupled pieces first (cheap to
revert, nothing else depends on their state), then the pieces that consume
from the database, then the piece that serves the schema-dependent API,
and only at the very end touch the schema itself — because everything
above it needs the schema to stay put while its own image rolls back.

**Rolling back an image never rolls back the schema.** Alembic has no
equivalent of the SaaS side's "migrations run automatically on container
start" behavior baked into every service — `migrate` is the only thing that
ever runs a migration, and it runs forward-only (`alembic upgrade head`, see
`apps/migrate/Dockerfile`'s `CMD`). Rolling `api`/`worker`/`scheduler`/
`scrapers` back to an older image does not touch `alembic_version` at all;
those services just start talking to whatever schema is currently live.
That is safe when the schema change was additive (older code simply doesn't
reference the new column/table) and unsafe when it was destructive or
renaming, for exactly the same reason the SaaS repo's own
`deploy/railway/ROLLBACK.md` states it for Prisma: **the recovery path for a
bad schema change is restore-from-dump, never a hand-written down-migration
run under incident pressure.** Every migration in `alembic/versions/`
carries a `downgrade()` function because Alembic requires one to exist, but
"exists" and "safe to run against production" are not the same claim — a
downgrade function is written and reviewed for the shape of the schema
change, not rehearsed against production data the way a restore is.

## The RLS migration is a deploy-order hazard, not just a migration

Revision `f2a6c1d80b37` (`alembic/versions/f2a6c1d80b37_rls_on_partitions.py`)
closes security review finding A1: four monthly-partitioned tables
(`request_attempts`, `price_observations`, `price_alert_events`,
`webhook_events`) had row-level security enabled on their *parent* tables
only. Postgres enforces the policies of the relation a query actually
names, and a partition created by `CREATE TABLE ... PARTITION OF` starts
with none of its own — so a query against a specific monthly partition
(rather than the parent) returned rows from every workspace, not just the
caller's, because `crawmatic_app` already holds `SELECT` on every table in
`public` (`scripts/sql/rls_roles.sql` §4) and nothing was narrowing that
grant back down at the partition level.

**This means the deploy-order rule above is not a nice-to-have for this
migration, it is the fix.** An `api` (or `worker`, or `scheduler`) built
after this migration was written *assumes* RLS is enforced on every table
it queries — that assumption is the entire security model this migration
exists to make true. If the new `api` image is deployed while `migrate`
has not yet applied `f2a6c1d80b37`, the new api is running with the
same code, same trust assumptions, and same *belief* that row-level
security is protecting cross-tenant reads, against a database where the
partition-level policies have not yet been created. That is not a
degraded feature or a 500 error — it is the exact defect this migration
fixes, still open, silently, for however long the gap lasts. **`migrate`
completing successfully, and specifically reaching `f2a6c1d80b37`, must be
confirmed before `api` starts serving traffic on this branch.** Use
`GET /version`'s `db_migration_head` (below) as that confirmation, not an
assumption that "the migrate job probably finished."

The same logic applies in reverse during a rollback: rolling `api` back to
an image from *before* `f2a6c1d80b37` existed is safe regardless of whether
the migration has been applied (older code never assumed partition-level
RLS, so it neither depends on it nor is weakened by its absence) — this is
the "additive" case from the decision principle above, just expressed as a
security invariant instead of a missing column. What is never safe is
downgrading past `f2a6c1d80b37` on the database side while any deployed
`api`/`worker`/`scheduler` image still assumes it holds; the migration's own
`downgrade()` restores the leaky pre-migration state on purpose (see the
migration's docstring: "It is never the right answer to a production
incident; roll forward").

## `/version`: what it can and can't confirm during a rollback

`GET /version` (`apps/api/app/routers/version.py`) reports `git_sha`,
`code_migration_head` (resolved from `alembic/versions/*.py` on disk, via
`ScriptDirectory.get_current_head()`), `db_migration_head` (read live from
`alembic_version.version_num`), and `migration_heads_match`. During a
rollback this is the fastest way to answer "did `migrate` actually reach
the head I think it did" — hit it after the `migrate` step and confirm
`db_migration_head` is `f2a6c1d80b37` (or later) before deploying `api`,
per the section above.

**Its `git_sha` field has a known gap that matters for "which image am I
rolling back TO."** `git_sha` reads `GIT_SHA` (set at build time by this
repo's own `images` CI job) or falls back to `RAILWAY_GIT_COMMIT_SHA`,
which Railway injects automatically — but only for a GitHub-connected
deploy. A CLI-uploaded deploy (`railway up` from a local checkout, the same
shape the SaaS repo's `deploy.sh` uses for its own services) carries neither
variable, so a CLI-uploaded engine service reports `git_sha: "unknown"`.
Concretely: if this branch's services were ever deployed via `railway up`
rather than through the CI `images` job, `/version` cannot tell you which
commit is actually running — you have to fall back to whatever provenance
record was kept outside the running system (the SaaS repo's own
`deploy/railway/README.md` "Engine release provenance" table is exactly
this kind of external record, kept because `/version` alone can't be
trusted for CLI-uploaded deploys). Don't treat `git_sha: "unknown"` as a
bug to route around during an incident — treat it as the signal that you
need the external record instead.

## Post-deploy gate

As of this branch, the only health endpoint this repo exposes is
**`/health`** (`apps/api/app/main.py`) — an unauthenticated, dependency-free
liveness probe that deliberately does not touch the database, Redis, or
Scrapyd (contracts/health.md, SPEC-01), and is the compose healthcheck
every other service's `depends_on: pgbouncer: condition: service_started`
ultimately traces back to for the `api` tier specifically. Use it as the
post-deploy gate for `api`: don't consider the `api` step of a rollout (or
rollback) complete until `/health` returns 200 on the new deployment.

**`/ready` (`apps/api/app/routers/ready.py`) now exists on this branch** —
an unauthenticated readiness probe that checks the database (`SELECT 1`)
and Redis (`PING`), each independently timeboxed, returning 200 only when
both are reachable and 503 otherwise. Because `/health` is intentionally
shallow (no DB touch, per SPEC-01), it cannot answer the specific
question this document cares about — "has `migrate` actually reached the
head `api` expects" — the way a database-aware readiness check can. Use
`/ready` as the post-deploy readiness gate for `api`: don't consider the
`api` step of a rollout (or rollback) complete until `/ready` returns 200
on the new deployment, and treat `/health` alone as insufficient proof
that it is safe to route traffic to a new `api` deployment on this
branch.

## See also

- `docker-compose.yml` — the `migrate` service's own comment is the source
  for "no app service runs migrations at startup; they only depend on
  pgbouncer."
- `alembic/versions/f2a6c1d80b37_rls_on_partitions.py` — the migration
  itself, including the full defect writeup and why it is idempotent and
  safe to re-run.
- `apps/api/app/routers/version.py` — `/version`'s implementation and the
  `git_sha: "unknown"` fallback behavior.
- `docs/ops/RUNBOOK_STOP_DISPATCH_AND_SPEND.md` — the companion runbook for
  stopping dispatch/spend without corrupting in-flight jobs; read alongside
  this one if a rollback is happening because something is actively
  spending money it shouldn't.
- The SaaS repo's `deploy/railway/ROLLBACK.md` — the equivalent document for
  the other repo in this platform, including its own decision table and the
  reasoning this document borrows for "rolling back an image never rolls
  back the schema."
