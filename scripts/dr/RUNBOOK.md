# Disaster Recovery Runbook — Crawmatic engine + SaaS

EPA task **W5.4** (READY-010, plan §P0.8). Written 2026-08-25.
Rewritten 2026-09-08 by **C10 (F21)**: the backup now runs *inside Railway* on
the private network, and this host keeps a pulled third copy.
Owner-approved objectives: **RPO ≤ 15 minutes, RTO ≤ 2 hours.**

This runbook describes what is **actually installed and proven** on the ops
host, and — separately and explicitly — what is **not**. Read §2 before
quoting an RPO number to anyone.

---

## 0. The production PostgreSQL major — DETERMINED, not assumed (2026-09-08)

Three files in this repo have an opinion about the PostgreSQL version and they
do not agree, so C10 read it out of the artifact that cannot be wrong: the
newest stored dump.

```console
$ gpg --decrypt .../sets/set-20260907T200001Z/engine.dump.gpg | head -c 512 | strings
PGDMP
railway
18.4 (Debian 18.4-1.pgdg13+1)        # server the dump was taken FROM
18.4 (Ubuntu 18.4-1.pgdg24.04+1)     # the pg_dump that took it

$ jq '.targets | map_values(.server_version)' .../set-20260907T200001Z/manifest.json
{ "engine": "18.4 (Debian 18.4-1.pgdg13+1)",
  "saas":   "18.6 (Debian 18.6-1.pgdg13+2)" }
```

**Finding: production is PostgreSQL 18 on both databases.** The plan's
tech-stack line ("PostgreSQL 18") is correct; **`docker-compose.yml`'s
`postgres:17.5-bookworm` pin is STALE** and is the outlier. Consequences,
already implemented:

* `apps/dr-backup/Dockerfile` pins `postgres:18-alpine` — the MAJOR from this
  finding, the PATCH deliberately floating, because `pg_dump` refuses a server
  newer than itself and the two production databases are already at different
  patch levels (18.4 / 18.6). A pinned patch would turn a provider maintenance
  window into a silent backup outage.
* `scripts/dr/verify_restore.sh` restores into `postgres:18-alpine`.
* `scripts/dr/rehearse_upgrade.sh` does not trust ANY of the three files: it
  re-reads the major from the manifest and cross-checks it against the dump's
  own `pg_restore -l` TOC header on every run, and alerts when the compose pin
  disagrees. Bumping the compose pin is a one-line follow-up owned outside
  this task.

---

## 1. What exists

| Piece | Path | Schedule |
|---|---|---|
| **Backup, inside Railway, on the private network** | `apps/dr-backup/` (`Dockerfile`, `backup.sh`, `serve.py`, `railway.json`) | every 4 h — Railway cron `0 */4 * * *` (**not yet provisioned — OWNER GATE §7**) |
| **Pull of the newest encrypted set to this host** | `scripts/dr/backup_prod.sh --mode pull` | daily (`/etc/cron.d/crawmatic-dr`) |
| Legacy host-side dump over the PUBLIC endpoint | `scripts/dr/backup_prod.sh --mode dump` | the automatic fallback until the gate above is executed |
| Restore verification into a throwaway PG 18 | `scripts/dr/verify_restore.sh` | daily 03:20 UTC |
| Host credential-permission sweep | `scripts/security/check_file_permissions.sh --mode host` | daily 04:10 UTC |
| Shared library (credentials, snapshots, retention) | `scripts/dr/dr_lib.sh` | — |
| Schedule installer / remover | `scripts/dr/install_schedule.sh` | — |

Artifacts live under `/srv/crawmatic/backups/dr/` (dir 0700, every file 0600):

```
sets/set-<UTC>/engine.dump.gpg      pg_dump -Fc, AES-256
sets/set-<UTC>/saas.dump.gpg        pg_dump -Fc, AES-256
sets/set-<UTC>/sidecars.tar.gz.gpg  netledger buffer + result spool + evidence listing
sets/set-<UTC>/manifest.json        per-table row counts + content checksums + sha256
                                    + alembic head + bytes_exported/bytes_on_wire
                                    + the sidecar inventory + the config snapshot
sets/set-<UTC>/SHA256SUMS
reports/verify-set-<UTC>.md         verdict + every assertion + step timings
reports/config-set-<UTC>.json       config snapshot, restored out of the manifest
reports/backup-set-<UTC>.json       the measured report posted to /admin/ops/backup-report
```

### Why the backup moved inside Railway (C10, F21)

The 2026-09 deep dive measured **0.853 GB/day of idle egress** from the
production Postgres, essentially all of it this backup pulling two full
logical dumps across the PUBLIC TCP proxy every four hours. The dump now runs
in a service inside the same Railway project, so the bytes cross
`*.railway.internal` and never leave the private network. `backup.sh` refuses
to start if any target's `PGHOST` is not a `*.railway.internal` name — the
saving is enforced, not hoped for — and every run records
`bytes_exported` (plaintext, counted in the pipe before encryption) and
`bytes_on_wire` (this network namespace's own `/proc/net/dev` counters), both
posted to `/admin/ops/backup-report`. `tests/unit/test_dr_backup_script_lint.py`
holds the same rule statically.

There is a **second, older** backup job on this host,
`/etc/cron.d/crawmatic-saas-backup` → `saas/deploy/railway/backup-prod-db.sh`,
which writes unencrypted nightly SaaS dumps to `/srv/crawmatic/backups/saas/`.
It is deliberately **left running**: it is an independent implementation, and
two independent backup paths is a feature during a recovery, not duplication
to tidy away. It is, however, **not** encrypted and **not** restore-verified —
treat `backups/dr/` as the recovery point of record.

---

## 2. RPO and RTO — the honest statement

### RTO — **met, with measured evidence**

| | Measured |
|---|---|
| Full backup of both databases | **50 s** and **49 s** (drills 1 and 2, 2026-08-25) |
| Full decrypt + restore + verify of both databases | **18 s** and **16 s** |
| End-to-end backup→restore→assert cycle | **≈ 66 s** |

Against an RTO of 2 hours that is roughly **two orders of magnitude of
headroom** — even multiplying by 10 for a bad day (a cold host, a bigger
database, a human reading this document first), the objective holds.

Caveat worth stating: this measures *restore of the data*. It does **not**
measure provisioning a replacement database, repointing services, or DNS.
That end-to-end path is the manual disaster exercise, which is an **owner
gate** (§6).

### RPO — **4 hours. Stated as a number, not a target.**

> **RPO = 4 h.** The backup runs every 4 hours (Railway cron `0 */4 * * *`),
> so a failure 3 h 59 m after the last successful set loses that window of
> writes. The approved objective is 15 minutes; **it is not met**, and moving
> the job inside Railway did not change that — it changed what the job costs,
> not how often it can run.

This is not a tuning oversight and raising the frequency does not fix it:

* These are **full logical dumps**. Each cycle moves the entire engine
  database (≈ 127 MB, 19 MB compressed) and SaaS database (≈ 33 MB, 2.6 MB
  compressed) and takes ~50 seconds. Running that every 15 minutes means 96
  full dumps a day.
* Inside Railway those bytes are free (private network) but not weightless:
  96 sets/day at ~33 MB is ~3 GB/day of volume churn before retention, and
  the pulled copy on this host — at ~97 % disk — cannot absorb it. The
  scripts refuse to run below their free-space floor for exactly this reason.
* Even at 15-minute dumps the true RPO would still be 15 minutes of lost
  writes. **A 15-minute or better RPO requires continuous WAL archiving or
  provider point-in-time recovery** — a property of the database provider, not
  something a cron job can synthesise.

**OWNER GATE — closing the RPO gap:** enable Railway (or the provider's)
point-in-time recovery / WAL archiving to off-site immutable storage. Until
that is on, the truthful statement for the READY register is:

> RPO ≤ 15 min is **NOT met**. Delivered: **RPO ≤ 4 h**, taken inside Railway
> on the private network, encrypted, restore-verified daily, with a pulled
> third copy on this host. RTO ≤ 2 h is **met** with measured margin.

### Residual risk — where the copies actually live

Stated verbatim, because "we have an off-host backup now" is the sentence this
paragraph exists to prevent:

> The second location is the same Railway account and region; losing the
> account loses both copies — the host pull copy is the third location; the
> gpg passphrase must be held in the owner's password manager, not only on
> this host.

Concretely, after C10 there are three copies and they fail together in one
scenario each:

| # | Copy | Lost when |
|---|---|---|
| 1 | The production databases | the database is corrupted or deleted |
| 2 | `dr-backup`'s Railway volume, 4-hourly / daily / weekly | **the Railway account or region is lost — takes #1 with it** |
| 3 | This host's pulled sets, last 2 days | this host is lost |

Copy #2 is not an independent location from copy #1 in the failure mode that
matters most (account compromise, account closure, provider-region loss). That
is what copy #3 is for, and why the pull is not optional — and it is still only
2 days deep. Genuine off-provider, immutable retention remains **owner gate 2**
in §6.

---

## 3. Encryption and key custody — the honest statement

Dumps are encrypted with `gpg --symmetric --cipher-algo AES256`, and
`pg_dump` writes to a pipe, never to a file, so **no plaintext copy of
production data is ever written to this host's disk**.

The passphrase is a 48-byte random value in `/root/.crawmatic-dr/backup.key`
(0600, root). That means:

* ✅ It protects the dumps against being **copied off this host** — a
  mis-scoped rsync target, a stolen disk image, a shared filesystem, a backup
  of the backup.
* ❌ It protects **nothing** against an attacker who already has root here.
  The key and the ciphertext sit on the same machine.
* ❌ It is **not a durability control**. Losing this host loses the backups
  *and* the key.
* ❌ Since C10 the SAME passphrase also lives in Railway as
  `$DR_GPG_PASSPHRASE` (name only — the value is the owner's), because the
  in-Railway backup must encrypt with a key this host can decrypt with. That
  widens custody, it does not deepen it: **the gpg passphrase must be held in
  the owner's password manager, not only on this host** (and not only in a
  Railway variable, which is lost with the account).

**OWNER GATES:**
1. **Off-site / immutable custody** of the encrypted sets (object storage with
   object-lock or equivalent). `saas/deploy/railway/backup-offsite-sync.sh`
   already exists and refuses to run until a destination is configured — it is
   the shape to reuse, pointed at `backups/dr/`.
2. **Key escrow** somewhere this host cannot reach, and migration of this
   passphrase (and every credential in `CREDENTIAL_INVENTORY.md`) into a
   **managed secret manager**.

Rotating the key is not "generate a new one": every existing set was encrypted
with the old passphrase. Procedure: keep the old key file, generate a new one
at a new path, point `DR_KEY_FILE` at it, and retain the old key until every
set encrypted under it has aged out of retention.

---

## 4. Alerting — the honest statement

`verify_restore.sh` exits **non-zero** and prints a line prefixed
**`DR-ALERT`** on stderr for every failed assertion. `backup_prod.sh` does the
same. Cron is configured `MAILTO=root`, so the output lands in root's local
mailbox if an MTA is running.

**A mailbox is not an alert.** There is no pager, no on-call rotation, and no
alert router on this host, so today the honest description is:

> Failure detection: automated and loud. Failure **notification**: a log file
> and a local mailbox that a human must choose to look at.

**OWNER GATE — real alerting:** route these non-zero exits to whatever channel
the team actually watches. The hook is deliberately trivial: any wrapper that
runs the script and reacts to `$?` works. The existing
`saas/deploy/ops/alert-cron.sh` (state-change email, `/etc/crawmatic/ops-alerts.env`)
is the nearest existing mechanism to extend.

Until then, check manually:

```bash
tail -50 /var/log/crawmatic-dr-verify.log
ls -t /srv/crawmatic/backups/dr/reports | head -3
grep -l 'Verdict: \*\*FAIL' /srv/crawmatic/backups/dr/reports/*.md
```

---

## 5. Operating the system

```bash
# state of everything
scripts/dr/install_schedule.sh --status

# pull the newest encrypted set from the dr-backup service (the normal path)
scripts/dr/backup_prod.sh --mode pull

# take a backup FROM THIS HOST over the public endpoint (the fallback; loud,
# billable egress — only until the dr-backup service exists)
scripts/dr/backup_prod.sh --mode dump

# whichever of the two applies (pull if /root/.crawmatic-dr/pull.env exists)
scripts/dr/backup_prod.sh

# verify the newest set (or a specific one) — restores the databases AND the
# sidecars, asserts migration-head equality, times every step
scripts/dr/verify_restore.sh
scripts/dr/verify_restore.sh --set set-20260825T225438Z

# remove the schedule (backups and key are untouched)
scripts/dr/install_schedule.sh --uninstall
```

### Sidecars — what a database dump does not contain

A logical dump of Postgres is not the whole recovery point. Three things live
outside it, and a restore that ignores them silently loses work that was
already paid for:

| Sidecar | Source | Why it matters |
|---|---|---|
| `netledger_buffer` | `$NETLEDGER_BUFFER_PATH` (SQLite/WAL) | sub-resource and close events not yet flushed into the ledger: **money already spent that is in no table yet** |
| `result_spool` | `$SCRAPE_RESULT_SPOOL_PATH` (B1, SQLite/WAL) | scrape results written durably before persistence: **pages already fetched and paid for** |
| `evidence_listing` | `$OFFER_EVIDENCE_STORE_DIR` (C5) | the *listing* of evidence blobs (not the blobs — they are large and live on their own volume), so a restore can prove exactly which hashes existed |
| `config_snapshot` | the producing service's environment | variable NAMES for everything plus VALUES for a declared non-secret allowlist — restored to `reports/config-<set>.json` |

Both SQLite sidecars are captured with `sqlite3.backup()`, never a file copy:
copying a live WAL database while a writer holds it yields a torn snapshot.
`verify_restore.sh` restores each one, runs `PRAGMA integrity_check` and
reports the queued row counts.

**A sidecar that is not captured is recorded as absent WITH A REASON** in the
manifest and reported as a `SKIP` in the drill — never as a silent pass. The
usual reason is the Railway volume carrying those queues being mounted on the
scraper services and not on `dr-backup` (a Railway volume attaches to exactly
one service); resolving that is part of the owner gate in §7.

### Restoring for real

1. Choose a set and confirm it verifies: `scripts/dr/verify_restore.sh --set <set>`.
   **Never restore a set that has not just verified.**
2. Decrypt and restore into the real target (this is the only step that
   differs from the drill):

   ```bash
   # PG* must point at the TARGET, never at production-by-accident
   gpg --batch --quiet --pinentry-mode loopback \
       --passphrase-file /root/.crawmatic-dr/backup.key \
       --decrypt /srv/crawmatic/backups/dr/sets/<set>/engine.dump.gpg \
     | /usr/lib/postgresql/18/bin/pg_restore --no-owner --no-privileges -d <target-db>
   ```
3. Re-run the row-count comparison against `manifest.json`, and confirm the
   restored `alembic_version` equals `manifest.targets.engine.alembic_head`,
   before declaring the restore good. `verify_restore.sh` asserts both.
4. **If — and only if — you are rebuilding a database by REPLAYING THE
   MIGRATION CHAIN onto an empty database** (rather than restoring a dump),
   read the next section first. A replay is silently lossy in a way a restore
   is not.
5. Restore the sidecars: unpack `sidecars.tar.gz.gpg` and put
   `netledger_buffer.sqlite3` / `result_spool.sqlite3` back at
   `$NETLEDGER_BUFFER_PATH` / `$SCRAPE_RESULT_SPOOL_PATH` **before** starting
   the scrapers, so their flushers drain them instead of starting from empty.
   Both replays are idempotent by key (`network_request_id`, `attempt_uuid`),
   so replaying an event that already landed is safe; skipping one is not.
   Then diff `evidence_listing.txt` against the evidence volume to see which
   blobs, if any, did not survive.

#### Replaying the migration chain onto an empty database — the RLS no-op trap

Recorded 2026-09-08 (B10 rehearsal, fixed at source by B2-fix1 for the one
migration that mattered; the rest are documented here because they are
already applied in production and will never run there again).

Production runs **fail-closed row-level security**, and `crawmatic_migrate` is
not a `BYPASSRLS` role. PostgreSQL applies a table's RLS policy to
`SELECT`/`INSERT`/`UPDATE`/`DELETE`, so DML issued by a migration that runs
*after* the policy exists matches **zero rows** and reports success. In
production this is harmless: each of these migrations ran before its table's
policy existed. **On a fresh restore that replays the whole chain as
`crawmatic_migrate`, every one of them silently backfills nothing.**

The known-latent set, pinned in `verify_restore.sh`
(`DR_RLS_BACKFILL_KNOWN`) and asserted on every drill so it cannot grow
unnoticed:

| Migration | Statement | Table |
|---|---|---|
| `5b9a86717a66_normalise_api_key_status` | `UPDATE` | `api_keys` |
| `6e4a9c8f2d10_versioned_strategy_methods` | `UPDATE` | `domain_strategy_profiles` |
| `6e4a9c8f2d10_versioned_strategy_methods` | `UPDATE` / `DELETE` | `strategy_attempt_stats` |
| `6e4a9c8f2d10_versioned_strategy_methods` | `INSERT` | `domain_strategy_methods` |
| `6e4a9c8f2d10_versioned_strategy_methods` | `INSERT` | `scrape_profile_revisions` |

(`b6e5d1c94a72_dispatch_intent_node_and_state` was the sixth; B2-fix1 moved
its backfill into the `USING` expression of the column rewrite, which RLS
cannot filter. That is the pattern to copy for any new one.)

**Procedure for a full-chain replay** — pick one, in this order of preference:

1. **Restore the dump instead of replaying.** A `pg_restore` of a set carries
   the already-backfilled data and is not subject to any of this. This is why
   the drill restores rather than replays.
2. If the chain must be replayed, run it as a role that is not subject to the
   policies — a superuser, or `crawmatic_migrate` temporarily granted
   `BYPASSRLS`:

   ```bash
   psql -c 'ALTER ROLE crawmatic_migrate BYPASSRLS;'      # before the replay
   alembic -x db_url=... upgrade head
   psql -c 'ALTER ROLE crawmatic_migrate NOBYPASSRLS;'    # immediately after
   ```

   `BYPASSRLS` must not survive the maintenance window: leaving it on turns
   every later migration into an unpoliced one.
3. Whichever route was taken, verify the five backfills actually landed before
   handing the database back — e.g. `SELECT count(*) FROM api_keys WHERE
   status IS NULL;` must be 0, and `scrape_profile_revisions` must be
   non-empty for every profile that has one in the manifest's row counts.

Notes:
* The client MUST be PostgreSQL 18 (`/usr/lib/postgresql/18/bin`). The distro
  default `/usr/bin/pg_dump` is 16.x and cannot handle these servers. See §0
  for how that major was determined.
* `--no-owner --no-privileges` is applied at **restore** time only; the
  archives themselves are faithful (owners and ACLs retained), so they remain
  valid production rollback artifacts.
* Roles/globals are **not** in these dumps (`pg_dumpall -g` needs privileges
  the Railway application user does not have). Role provisioning is
  `scripts/provision_db_roles.sql`.

### Retention

**On the `dr-backup` volume** (`apps/dr-backup/backup.sh`): every 4-hourly set
for **2 days**, the newest set of each of the last **14 days**, the newest set
of each of the last **8 ISO weeks**; never fewer than 3 sets; never more than
`DR_MAX_BYTES` (8 GiB) in the store. Steady state ≈ 46 sets ≈ 1.5 GB.

**On this host** (`scripts/dr/backup_prod.sh --mode pull`): the last **2 days**
only — no dailies, no weeklies — never fewer than 3 sets, never more than
1 GiB. This host is the recent third copy, not the archive.

Both windows are one implementation (`dr_lib.sh:dr_prune_sets`) with different
parameters. Pruning runs before and after every run; the pre-run prune is what
makes room, and the job refuses to run at all below its free-space floor.

---

## 6. Open owner gates (restated for the READY register)

| # | Gate | Blocks |
|---|---|---|
| 1 | Provider PITR / WAL archiving | **RPO ≤ 15 min** — not met without it |
| 2 | Off-site, immutable retention of the encrypted sets | single-host loss = total loss |
| 3 | Off-host escrow of the backup passphrase | same |
| 4 | Managed secret manager + scheduled rotation | `CREDENTIAL_INVENTORY.md` |
| 5 | Real alert routing for `DR-ALERT` / non-zero exits | failure notification |
| 6 | One **manual disaster exercise** (region/account loss, real restore, services repointed) | GA |
| 7 | **Provision the `dr-backup` service** (§7): create it + its volume, set `$DR_GPG_PASSPHRASE` / `$DR_PULL_TOKEN` / the `*_PG*` references, run one manual backup, run `verify_restore.sh` against it, confirm public egress drops toward 0 | the 0.853 GB/day egress saving, and the private-network backup itself |
| 8 | Mount the queue volume on `dr-backup` (or an equivalent) so the netledger buffer and result spool are actually captured | sidecars are `SKIP`ped in every drill until then |

---

## 7. OWNER GATE — provisioning `dr-backup` (C10 step 3, executed inside C11)

**Status: DEFERRED. Nothing in this section has been run.** No Railway service
was created, no variable was set, no manual backup was taken and no egress was
measured by the task that wrote this file (EPA C10; owner gates are deferred
per the run's ASSUMPTIONS.md answer 2, and the destructive firewall forbids
creating services, changing variables or dumping production).

Everything below is the exact sequence to execute when the gate is taken.

### Step 1 — create the service and its volume

```bash
# In the ENGINE Railway project (69dc4bda-0d97-4290-a82f-822ed97d3fb8),
# environment production, with the Railway-2 token.
railway add --service dr-backup
railway volume add --service dr-backup --mount-path /backups   # ~8 GiB
```

`apps/dr-backup/railway.json` already carries the build (Dockerfile at
`apps/dr-backup/Dockerfile`, build context = repo root), the cron schedule
`0 */4 * * *`, `restartPolicyType: NEVER`, and the `/backups` mount.

**Decide the shape now, because a Railway volume attaches to exactly ONE
service and a cron service is not running between invocations:**

* **Shape A (recommended) — one always-on service.** Drop `cronSchedule`, set
  the start command to `/app/serve.py` and `DR_SCHEDULE_LOOP=1`. The same
  process serves `/backups` to the ops host over the private network *and*
  runs `backup.sh` every 4 h in-process.
* **Shape B — cron only, no pull.** Keep `railway.json` as written. The
  volume then has no HTTP surface, so `scripts/dr/backup_prod.sh --mode pull`
  has nothing to talk to and this host keeps no third copy. Only choose this
  if off-provider retention is being solved another way.

### Step 2 — set the variables (NAMES here; values are the owner's)

```bash
railway variables --service dr-backup --set DR_GPG_PASSPHRASE=...   # SAME passphrase as /root/.crawmatic-dr/backup.key, or the sets stop being mutually decryptable
railway variables --service dr-backup --set DR_PULL_TOKEN=...       # random 32+ bytes; the ops host presents it as a bearer token
railway variables --service dr-backup --set DR_REPORT_URL=http://<api-service>.railway.internal:8000
railway variables --service dr-backup --set DR_REPORT_TOKEN=...     # the engine's SAAS_SERVICE_TOKEN value

# Per-target database variables, as REFERENCES to the private fields:
railway variables --service dr-backup --set ENGINE_PGHOST='${{postgres.RAILWAY_PRIVATE_DOMAIN}}'
railway variables --service dr-backup --set ENGINE_PGPORT='5432'
railway variables --service dr-backup --set ENGINE_PGUSER='${{postgres.PGUSER}}'
railway variables --service dr-backup --set ENGINE_PGPASSWORD='${{postgres.PGPASSWORD}}'
railway variables --service dr-backup --set ENGINE_PGDATABASE='${{postgres.PGDATABASE}}'
# …and the SAAS_* five, referencing the SaaS project's Postgres service.
```

`backup.sh` **refuses to run** if any `*_PGHOST` is not a
`*.railway.internal` name. That is deliberate: a public host there silently
reintroduces the 0.853 GB/day of egress this service exists to remove.

If the sidecar queues are wanted in the sets (§5 Sidecars), also mount the
volume that carries them and set `NETLEDGER_BUFFER_PATH`,
`SCRAPE_RESULT_SPOOL_PATH` and `OFFER_EVIDENCE_STORE_DIR`. Until then those
sidecars are recorded absent-with-a-reason and the drill reports them as
`SKIP`.

### Step 3 — one manual backup, then verify it

```bash
railway run --service dr-backup /app/backup.sh          # one run, watch the log
railway logs --service dr-backup | tail -40             # expect "=== dr-backup run set-… OK ==="
```

Then on this host, once the pull config exists:

```bash
install -d -m 700 /root/.crawmatic-dr
cat > /root/.crawmatic-dr/pull.env <<'EOF'
DR_PULL_BASE_URL=http://dr-backup.railway.internal:8080   # or the https edge domain
DR_PULL_TOKEN=...                                          # the DR_PULL_TOKEN value
EOF
chmod 600 /root/.crawmatic-dr/pull.env

scripts/dr/backup_prod.sh --mode pull        # fetches the newest set, verifies SHA256SUMS
scripts/dr/verify_restore.sh                 # restores it, sidecars included, timed
```

`backup_prod.sh` refuses a plain-`http` base URL unless the host is
`*.railway.internal`, so this cannot quietly become an unencrypted transfer
over the public internet.

### Step 4 — confirm the egress actually dropped

The point of the whole task. In Railway's metrics for the production Postgres
service, the day after step 3:

* **Baseline to beat:** 0.853 GB/day idle egress (2026-09 deep dive).
* **Expected after:** ≈ 0 — the only remaining public traffic from that
  database should be application traffic, not backups.
* Cross-check against the reports this host now writes:
  `jq '.totals' /srv/crawmatic/backups/dr/reports/backup-set-*.json` —
  `bytes_on_wire` for a private run is private-network traffic, and
  `private_network: true` says which side of the move each report came from.

If egress does **not** drop, the most likely cause is that
`/etc/cron.d/crawmatic-dr` still runs `backup_prod.sh` in `--mode dump`:
creating `/root/.crawmatic-dr/pull.env` is what switches it, and the log says
which mode ran on every invocation.

---

## 8. Evidence

* Drill transcripts: `/srv/crawmatic/evidence/w54-drill-1-2026-08-25.txt`,
  `/srv/crawmatic/evidence/w54-drill-2-2026-08-25.txt`
* Verification reports: `/srv/crawmatic/backups/dr/reports/`
* Credential inventory: `/srv/crawmatic/evidence/CREDENTIAL_INVENTORY.md`
* Rotation runbook (A3): `/srv/crawmatic/evidence/OWNER_RUNBOOK_credential_rotation.md`
* Prior manual drill (A4): `/srv/crawmatic/evidence/restore-drill-2026-08-25.md`
* Secret residue scan (A3): `/srv/crawmatic/evidence/secret-scan-2026-08-25.md`
