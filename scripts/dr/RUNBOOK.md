# Disaster Recovery Runbook — Crawmatic engine + SaaS

EPA task **W5.4** (READY-010, plan §P0.8). Written 2026-08-25.
Owner-approved objectives: **RPO ≤ 15 minutes, RTO ≤ 2 hours.**

This runbook describes what is **actually installed and proven** on the ops
host, and — separately and explicitly — what is **not**. Read §2 before
quoting an RPO number to anyone.

---

## 1. What exists

| Piece | Path | Schedule |
|---|---|---|
| Encrypted logical backup of both prod DBs | `scripts/dr/backup_prod.sh` | every 4 h (`/etc/cron.d/crawmatic-dr`) |
| Restore verification into a throwaway PG 18 | `scripts/dr/verify_restore.sh` | daily 03:20 UTC |
| Host credential-permission sweep | `scripts/security/check_file_permissions.sh --mode host` | daily 04:10 UTC |
| Shared library (credentials, snapshots, retention) | `scripts/dr/dr_lib.sh` | — |
| Schedule installer / remover | `scripts/dr/install_schedule.sh` | — |

Artifacts live under `/srv/crawmatic/backups/dr/` (dir 0700, every file 0600):

```
sets/set-<UTC>/engine.dump.gpg    pg_dump -Fc, AES-256
sets/set-<UTC>/saas.dump.gpg      pg_dump -Fc, AES-256
sets/set-<UTC>/manifest.json      per-table row counts + content checksums + sha256
sets/set-<UTC>/SHA256SUMS
reports/verify-set-<UTC>.md       verdict + every assertion
```

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

### RPO — **NOT met by this automation. 4 hours, not 15 minutes.**

The cron cadence is every 4 hours, so a failure 3 h 59 m after the last
successful set loses that window of writes.

This is not a tuning oversight and raising the frequency does not fix it:

* These are **full logical dumps**. Each cycle transfers the entire engine
  database (≈ 127 MB, 19 MB compressed) and SaaS database (≈ 33 MB, 2.6 MB
  compressed) across the Railway TCP proxy and takes ~50 seconds. Running that
  every 15 minutes means 96 full dumps a day.
* The ops host is at **~91 % disk** with ~7 GB free. Ninety-six sets a day at
  22 MB is ~2 GB/day before retention, on a filesystem that cannot absorb it —
  and a backup job that fills the ops host is an outage, not a backup. The
  scripts refuse to run below 2 GB free for exactly this reason.
* Even at 15-minute dumps the true RPO would still be 15 minutes of lost
  writes. **A 15-minute or better RPO requires continuous WAL archiving or
  provider point-in-time recovery** — a property of the database provider, not
  something a cron job on this host can synthesise.

**OWNER GATE — closing the RPO gap:** enable Railway (or the provider's)
point-in-time recovery / WAL archiving to off-site immutable storage. Until
that is on, the truthful statement for the READY register is:

> RPO ≤ 15 min is **NOT met**. Delivered: RPO ≤ 4 h, host-local, encrypted,
> restore-verified daily. RTO ≤ 2 h is **met** with measured margin.

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

# take a backup right now
scripts/dr/backup_prod.sh

# verify the newest set (or a specific one)
scripts/dr/verify_restore.sh
scripts/dr/verify_restore.sh --set set-20260825T225438Z

# remove the schedule (backups and key are untouched)
scripts/dr/install_schedule.sh --uninstall
```

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
3. Re-run the row-count comparison against `manifest.json` before declaring
   the restore good.

Notes:
* The client MUST be PostgreSQL 18 (`/usr/lib/postgresql/18/bin`). The distro
  default `/usr/bin/pg_dump` is 16.x and cannot handle these servers.
* `--no-owner --no-privileges` is applied at **restore** time only; the
  archives themselves are faithful (owners and ACLs retained), so they remain
  valid production rollback artifacts.
* Roles/globals are **not** in these dumps (`pg_dumpall -g` needs privileges
  the Railway application user does not have). Role provisioning is
  `scripts/provision_db_roles.sql`.

### Retention

Keeps every set from the last 24 h, plus the newest set of each of the last
7 days, never fewer than 3 sets, never more than 1 GiB total. Steady state is
~14 sets ≈ 310 MB. Pruning runs before and after every backup; the pre-run
prune is what makes room, and the job refuses to dump at all below 2 GB free.

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

---

## 7. Evidence

* Drill transcripts: `/srv/crawmatic/evidence/w54-drill-1-2026-08-25.txt`,
  `/srv/crawmatic/evidence/w54-drill-2-2026-08-25.txt`
* Verification reports: `/srv/crawmatic/backups/dr/reports/`
* Credential inventory: `/srv/crawmatic/evidence/CREDENTIAL_INVENTORY.md`
* Rotation runbook (A3): `/srv/crawmatic/evidence/OWNER_RUNBOOK_credential_rotation.md`
* Prior manual drill (A4): `/srv/crawmatic/evidence/restore-drill-2026-08-25.md`
* Secret residue scan (A3): `/srv/crawmatic/evidence/secret-scan-2026-08-25.md`
