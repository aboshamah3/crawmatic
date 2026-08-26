# Data Retention Policy — Program Document

EPA task W5.5-GA, item C (report §10). **Status: DRAFT FOR OWNER/COUNSEL
RATIFICATION.** Every duration, legal basis, and irreversible-action
threshold in this document is marked **PENDING OWNER/COUNSEL** (§15.5 of
the governing plan) — nothing here is silently-invented policy. What IS
authoritative here: the inventory of data classes, an honest account of
which retention/deletion *mechanisms already exist in code* (cited by real
path, not asserted), which do not, and which tables are structurally
append-only and therefore need a different deletion strategy than a row
`DELETE`.

This is the **program document** — the thing the owner ratifies. It does
not replace `libs/shared/app_shared/observations/EVIDENCE_RETENTION.md`
(W3.1's evidence-blob retention policy, already shipped and enforced by
`evidence_store.py`); it cross-references that document as the evidence
class's authoritative source and does not restate its content.

---

## 1. How to read this document

For each data class below:

* **Retention duration** — PENDING OWNER/COUNSEL unless a mechanism
  already enforces a concrete number in code today, in which case that
  number is cited (file + setting name) as the *current default*, not as
  a ratified policy. A cited default is still subject to owner/counsel
  sign-off — it is what the system does today, not necessarily what it
  should do.
* **Legal basis** — PENDING OWNER/COUNSEL placeholder. This system
  operates across jurisdictions (GCC-region merchants per
  `saas-wt-w51`'s Arabic/RTL/GCC work, `specs/018-arabic-rtl-gcc/`) whose
  data-retention and deletion-obligation regimes this document does not
  attempt to resolve. Do not treat any duration below as GDPR/PDPL-
  compliant until counsel confirms it.
* **Deletion mechanism** — cites the real, already-shipped mechanism
  where one exists, or states plainly "no mechanism exists" where none
  does.
* **Append-only?** — whether the table structurally rejects `UPDATE`/
  `DELETE` (a database trigger — the strongest form) or is merely an
  app-level convention (weaker; an operator with direct DB access can
  still violate it). This determines whether "delete an individual row"
  is even possible, or whether the only lawful deletion path is a
  **partition drop** (whole time-range) or an **owner-authorized
  exception** (a manual, logged, out-of-band operation bypassing the
  trigger).

---

## 2. Data class inventory

### 2.1 Raw HTML / evidence blobs

| | |
|---|---|
| **What** | The raw bytes an extraction strategy read offer facts from (`OfferObservation.raw_evidence_hash` / `price_observations.offer_raw_evidence_hash`), content-addressed by `sha256(bytes)`. |
| **Authoritative policy** | `libs/shared/app_shared/observations/EVIDENCE_RETENTION.md` (W3.1) — **this document extends it, does not duplicate it.** Read that file first. |
| **Current mechanism** | `evidence_store.py`'s `store_evidence`/`resolve_hash`/`replay` (write-once, read-many, integrity-checked on every read). **No deletion mechanism exists today** — W3.1's own doc states this explicitly under "What W5.5 adds later": *"A retention-by-age sweep (drop evidence past a configurable window)"* is listed as NOT YET BUILT. This W5.5-GA task does not build it either (out of scope — `scrape_core/extraction/` and the maintenance task wiring are fenced for this task); it is recorded here as an **open gap the owner must decide on before evidence accumulates without bound**, cross-referencing forward from the program document that names the gap to the evidence doc that owns the mechanism. |
| **Retention duration** | PENDING OWNER/COUNSEL. No default exists — evidence currently accumulates indefinitely (`./var/evidence` locally; production placement is itself an open deployment decision per the W3.1 doc). |
| **Legal basis** | PENDING OWNER/COUNSEL. |
| **Deletion mechanism** | None shipped. When built: filesystem object deletion keyed by hash, gated on no `price_observations.offer_raw_evidence_hash` row still referencing it (a hash referenced by a NOT-yet-retention-eligible observation must not be deleted out from under it — see §2.2's partition-drop-vs-rollup-coverage precedent for the shape this gate should take). |
| **Append-only?** | Write-once by convention (`evidence_store.py`), not DB-trigger-enforced — it is a filesystem store, not a table. |

### 2.2 `request_attempts` + partitions

| | |
|---|---|
| **What** | Per-fetch-attempt record (byte accounting, success/failure, origin). Monthly-partitioned by `created_at`. |
| **Current mechanism** | `libs/shared/app_shared/maintenance/registry.py`'s `PARTITIONED_TABLES` registers this table with `retention_setting="RETENTION_REQUEST_ATTEMPTS_DAYS"`, `feeds_rollups=False` (drops by age alone, FR-019). `libs/shared/app_shared/maintenance/retention.py::run_retention` does the actual `DROP TABLE IF EXISTS` per eligible whole partition (`partition_eligible`, FR-018 — a partition is eligible only when its **entire** range is past the cutoff). |
| **Retention duration** | Current *default* per code: whatever `Settings.RETENTION_REQUEST_ATTEMPTS_DAYS` resolves to (`app_shared/config.py` — not read here in detail per this task's fence on `config.py`; cite the setting name, not its value, without independently re-verifying the live default). **PENDING OWNER/COUNSEL ratification of that number as policy**, not just as a code default nobody signed off on. |
| **Legal basis** | PENDING OWNER/COUNSEL. |
| **Deletion mechanism** | Whole-partition `DROP TABLE` — never a bulk `DELETE` (SC-003, `retention.py`'s own docstring: *"never a bulk DELETE on a raw append-heavy table"*). |
| **Append-only?** | Partitioned + high-volume ("raw append-heavy" per the module's own docstring) but **not** trigger-enforced append-only — no `CREATE TRIGGER ... append_only` found on this table (contrast §2.5). Deletion is still partition-drop-only by *design choice* (performance/lock-avoidance), not by structural enforcement. |

### 2.3 Price observations

| | |
|---|---|
| **What** | `price_observations` — immutable extraction-attempt result (price, currency, comparability, plus the W3.1 `offer_*` superset). Monthly-partitioned by `scraped_at`. |
| **Current mechanism** | Same registry (`PARTITIONED_TABLES`), but `feeds_rollups=True` — the ONE entry requiring `rollups_cover` (`retention.py::rollups_cover`) to pass before a partition drops: every UTC date in the partition's range that had source observations must already have >=1 `variant_price_daily_rollups` row, or the partition is retained and reported `partitions_skipped_pending_rollups` (self-healing — re-checked next run, never silently dropped, research R7). |
| **Retention duration** | Current default per `Settings.RETENTION_PRICE_OBSERVATIONS_DAYS`. **PENDING OWNER/COUNSEL.** |
| **Legal basis** | PENDING OWNER/COUNSEL — this is the table an `OfferObservation`-driven pricing decision traces back to; a retention window here interacts directly with any dispute/audit-trail obligation, not just storage cost. |
| **Deletion mechanism** | Whole-partition `DROP TABLE`, verify-before-drop gated on rollup coverage. |
| **Append-only?** | No DB trigger found. Partitioned + append-heavy by convention, same posture as §2.2. |
| **Known coupling** | READY-013-h (`READY_REGISTER.md`) names a **known Jul-11→Aug-12 rollup backlog that never self-heals today**, which "keeps the retention safety-hold shut" — i.e. this table's retention is currently *blocked in practice* by an unrelated watermark bug, not by policy. That is a separate GA item (READY-013-h, not this task) but is recorded here because it directly gates whether `price_observations` retention can ever actually run. |

### 2.4 Alerts (`price_alert_events`)

| | |
|---|---|
| **What** | Alert-event log, partitioned by `created_at`. |
| **Current mechanism** | `PARTITIONED_TABLES` entry, `feeds_rollups=False`, `retention_setting="RETENTION_PRICE_ALERT_EVENTS_DAYS"`. Same whole-partition-drop mechanism as §2.2. |
| **Retention duration** | Current default per `Settings.RETENTION_PRICE_ALERT_EVENTS_DAYS`. **PENDING OWNER/COUNSEL.** |
| **Legal basis** | PENDING OWNER/COUNSEL. |
| **Deletion mechanism** | Whole-partition `DROP TABLE`. |
| **Append-only?** | No DB trigger found; convention only. |
| **Also registered, not yet live** | `webhook_events` (SPEC-16) is registered in `PARTITIONED_TABLES` ahead of its own migration and is currently absent from the schema — `table_exists` skips it cleanly (FR-002) until it lands. Listed here so the retention program covers it the moment it ships, rather than the registry silently getting ahead of a policy decision. |

### 2.5 Audit rows

#### 2.5.a `domain_lifecycle_audit`

| | |
|---|---|
| **What** | Domain-lifecycle transition audit trail (`libs/shared/app_shared/models/domain_playbooks.py::DomainLifecycleAudit`, migration `9f24d748ba13_domain_lifecycle_transitions_and_audit.py`). |
| **Current mechanism** | Not registered in `PARTITIONED_TABLES` — **no automated retention exists for this table today.** Not partitioned. |
| **Retention duration** | PENDING OWNER/COUNSEL. An audit trail's retention is usually driven by the *longest* applicable obligation (dispute window, regulatory audit period), not by storage cost — flag for counsel specifically, not a default-by-analogy to the raw data tables above. |
| **Legal basis** | PENDING OWNER/COUNSEL. |
| **Deletion mechanism** | None shipped. `domains/` and `scripts/domain_lifecycle.py` are fenced for this task (another worker's active lane) — this document names the gap; it does not propose a mechanism inside the fenced module. |
| **Append-only?** | App-level audit-log convention (a lifecycle transition is inherently a historical fact), **not** DB-trigger-enforced — no `CREATE TRIGGER` found on this table. |

#### 2.5.b `strategy_method_switches`

| | |
|---|---|
| **What** | Strategy-chain switch audit (`libs/shared/app_shared/models/strategy_switches.py::StrategyMethodSwitch`, migration `05dfda7cdbb7_rollup_watermark_and_strategy_switch_audit.py`). |
| **Current mechanism** | Not registered in `PARTITIONED_TABLES` — no automated retention. `strategy/` is fenced for this task. |
| **Retention duration / legal basis / deletion mechanism** | PENDING OWNER/COUNSEL / PENDING OWNER/COUNSEL / none shipped — same posture as 2.5.a. |
| **Append-only?** | App-level convention only. |

#### 2.5.c `match_audit_classifications`

| | |
|---|---|
| **What** | Match-identity classification history (`libs/shared/app_shared/models/match_audit.py`, EPA A6). |
| **Current mechanism** | App-enforced append-only invariant per the module's own comment (*"a new classification is always an [insert]"*) reinforced by a partial unique index (`uq_mac_match_id_current`) enforcing "at most one CURRENT classification per match" **in the database** — a real constraint, though the append-only property itself (no row is ever mutated once superseded) is an application convention around that index, not a `BEFORE UPDATE`/`BEFORE DELETE` trigger like §2.6 below. |
| **Retention duration / legal basis / deletion mechanism** | PENDING OWNER/COUNSEL / PENDING OWNER/COUNSEL / none shipped. |

### 2.6 Billing / ledger rows

This is the class where "append-only" stops being a convention and starts being a **database-enforced structural property** — retention here cannot be "delete old rows," full stop, without an owner-authorized exception path.

#### 2.6.a `network_operations` family (`network_operations`, `network_operation_allocations`, `network_operation_settlements`)

| | |
|---|---|
| **What** | The physical cost ledger (C1/C4/C5) — fleet-owned (no `workspace_id`), the durable record of physical resource consumption independent of any provider dashboard. |
| **Append-only?** | **TRUE, DB-trigger-enforced.** `libs/shared/app_shared/models/network_operations.py`: a closed `network_operations` row is protected by a `BEFORE UPDATE` trigger that rejects any update once `closed_at` is non-NULL (*"A closed physical operation is an append-only fact"*); `network_operation_settlements` has `trg_network_operation_settlements_append_only` explicitly rejecting **both** UPDATE and DELETE (*"network_operation_settlements is append-only"*, the model docstring: *"UPDATE and DELETE are both rejected by a trigger, so 'append-only' [...] is structural, not a convention"*). An allocation-total constraint trigger additionally re-checks correctness at COMMIT. |
| **Deletion mechanism** | **None can exist as a row `DELETE` against a live, closed operation — the trigger will reject it.** The only lawful paths are: (a) an **owner-authorized exception** that explicitly disables/bypasses the trigger for a scoped, logged, reversible-audit operation (not built — PENDING OWNER decision on whether this exception path should ever exist at all, given it directly undermines the guarantee the trigger provides); or (b) if this table is ever partitioned in the future, a whole-partition drop (not partitioned today — confirmed absent from `PARTITIONED_TABLES`). |
| **Retention duration / legal basis** | PENDING OWNER/COUNSEL — and this is the highest-stakes PENDING in this document: this is billing/cost evidence, likely subject to financial-record retention law (commonly multi-year, jurisdiction-dependent) that should be resolved with counsel before any deletion mechanism is even designed, let alone built. |

#### 2.6.b `provider_usage_records`

| | |
|---|---|
| **What** | Immutable, content-addressed (`sha256:` over exact export bytes) provider-reported usage evidence (C5, `libs/shared/app_shared/models/provider_usage.py::ProviderUsageRecord`, migration `7c2b9e5a41d6`). Fleet-owned. Re-importing the same file is a no-op; a genuinely different file always lands as new rows — never merged or summarized away (`netledger/reconcile.py`'s module docstring). |
| **Append-only?** | Evidence rows are never merged/mutated by design (app convention — a re-import of different bytes is new rows, not an update to old ones); no DB trigger confirmed on this specific table in the review for this task (distinct from §2.6.a's `network_operations` family, which IS trigger-enforced — do not conflate the two). |
| **Retention duration / legal basis / deletion mechanism** | PENDING OWNER/COUNSEL / PENDING OWNER/COUNSEL / none shipped. |

#### 2.6.c `cost_reservations` / `cost_budgets` / `fleet_cost_budgets` (C3)

| | |
|---|---|
| **What** | The cost-authorization store — reservations, budgets, entitlement evidence (`libs/shared/app_shared/costauth/`, `libs/shared/app_shared/models/cost_authorization.py`, migration `f1a7c02de5b4`). `costauth/` is **fenced** for this task (fresh W5.5-L1 work) — this document cites it for the retention inventory only, does not read deep into its logic beyond what's needed to classify the data. |
| **Append-only?** | **NOT append-only** — this is live operational state (a reservation transitions RESERVED -> settled/released; `service.py`: *"Settlement and release are compare-and-set on the reservation's own [state]"*). A TERMINAL (settled/released) reservation is the historical fact worth retaining; the mutable pre-terminal state is not itself audit evidence in the same sense as §2.6.a. |
| **Retention duration / legal basis / deletion mechanism** | PENDING OWNER/COUNSEL / PENDING OWNER/COUNSEL / none shipped. Because this table is mutable (not trigger-append-only), an eventual retention mechanism here is structurally simpler than §2.6.a's — ordinary age-based deletion of TERMINAL rows is possible without an owner-authorized trigger bypass. Still PENDING; noted so a future implementer doesn't assume the harder §2.6.a shape applies here too. |

### 2.7 Backups (incl. the W5.4 DR artifacts)

| | |
|---|---|
| **What** | Encrypted logical dumps of both prod DBs (`/srv/crawmatic/backups/dr/sets/set-<UTC>/{engine,saas}.dump.gpg` + manifest + `SHA256SUMS`), produced every 4h by `scripts/dr/backup_prod.sh`; restore-verified daily by `scripts/dr/verify_restore.sh` (`scripts/dr/RUNBOOK.md`, `scripts/dr/dr_lib.sh`). A second, independent, **unencrypted, non-restore-verified** nightly SaaS-only backup also exists (`/etc/cron.d/crawmatic-saas-backup` -> `saas/deploy/railway/backup-prod-db.sh`, writing to `/srv/crawmatic/backups/saas/`) — the DR runbook explicitly names `backups/dr/` as "the recovery point of record," not this second path. |
| **Current mechanism / retention budget** | `scripts/dr/RUNBOOK.md` §2 is the existing, ALREADY-HONEST retention/capacity statement for this class — cited here, not duplicated: RPO is **4 hours** (cron cadence), explicitly **not** the owner-approved 15-minute objective (*"NOT met by this automation"*), because (a) each cycle is a full logical dump (~127MB engine + ~33MB SaaS, ~22MB compressed combined) taking ~50s, and (b) the ops host was at **~91% disk with ~7GB free** at the time of writing, and the backup scripts **refuse to run below 2GB free** — a self-imposed retention/capacity floor already enforced in code, cited here rather than restated. RTO is met with roughly two orders of magnitude of headroom (measured: ~66s full backup-restore-assert cycle vs. a 2h objective) — see the runbook for the caveat that this measures data-restore only, not full service reprovisioning. |
| **Retention duration (how many historical backup SETS are kept, not RPO)** | **Not found in the reviewed scripts for this task** — `scripts/dr/dr_lib.sh` is named in the runbook as owning "retention" logic, but this task did not re-derive its exact sweep window (out of scope to re-audit W5.4's own implementation; cite the file, don't re-verify its number). **PENDING OWNER/COUNSEL** to confirm the actual configured sweep window against the disk-capacity constraint above — a retention window that doesn't respect the 2GB-free floor is a self-inflicted outage, not a policy choice, per the runbook's own reasoning. |
| **Legal basis** | PENDING OWNER/COUNSEL. |
| **Deletion mechanism** | `dr_lib.sh`'s retention sweep (cited, not re-verified — see above). |
| **Append-only?** | N/A — these are file-system artifacts, not DB rows. |

### 2.8 Logs

| | |
|---|---|
| **What** | Application/service logs (API, worker, scheduler, scraper processes) and CI/build logs. |
| **Current mechanism** | **No log-retention mechanism was found anywhere in this codebase during this task's review** (`LOG_RETENTION`, `logrotate`, and similar patterns returned zero matches across `.py`/`.md`/`.sh`). Log lifecycle today is whatever the deployment platform (Railway) and any ambient host `logrotate` provide — neither is a policy this repository controls or has documented. |
| **Retention duration / legal basis / deletion mechanism** | **PENDING OWNER/COUNSEL, entirely undecided.** This is the least-covered data class in this document and should be treated as a real gap for GA, not a formality — logs commonly carry PII (request paths, error messages that may echo user input) with no retention control at all today. |

---

## 3. Tenant export/deletion flow — sketch (not built)

No tenant-initiated export or deletion flow exists in this codebase today
(the SaaS-side `saas-wt-w51` worktree's own binding context,
`specs/019-hardening-launch/`, covers authorization hardening and trial-
abuse monitoring, not data export/erasure). Sketch, for owner/counsel
scoping before implementation:

1. **Trigger.** A tenant-initiated request (support ticket today; a
   self-service control is a product decision, not assumed here) names a
   `workspace_id`.
2. **Export.** A read pass over every table this document's §2 inventory
   marks as workspace-scoped (`WorkspaceScopedBase` subclasses — the same
   base class RLS itself keys on, per `scripts/rls_table_manifest.txt`'s
   manifest, cross-checked by `scripts/provision_db_roles.py --verify` in
   CI's `tenant-isolation` job) for that `workspace_id`, serialized to a
   deliverable format. **The evidence-blob class (§2.1) needs a second
   pass**: a `price_observations.offer_raw_evidence_hash` naming a blob
   under this workspace must be resolved through `evidence_store.replay`
   and included, not just the DB row — see W3.1's `EVIDENCE_RETENTION.md`
   "What W5.5 adds later" note on a capability flag gating who may
   trigger a replay; a tenant-facing export is exactly the case that flag
   was anticipated for.
3. **Deletion.** For each table: a plain workspace-scoped `DELETE` where
   the table is mutable and not append-only (§2.6.c-shaped tables); a
   **partition-drop-eligible wait** where the table is partitioned
   (§2.2-2.4) — a single tenant's rows cannot be surgically removed from
   a shared partition without either (a) waiting for that partition's
   natural age-out, or (b) a row-level `DELETE` that the retention
   module's own docstring explicitly rules out for these tables
   (`retention.py`: *"never a bulk DELETE on a raw append-heavy table"*)
   — this is a genuine, unresolved tension between "tenant asked to be
   forgotten now" and "this table's own performance/lock-avoidance
   design forbids row-level deletes," and is **PENDING OWNER/COUNSEL**
   to resolve, not assumed away here; and an **owner-authorized
   exception** where the table is trigger-append-only (§2.6.a) — the
   trigger bypass named there is exactly this case's hard path.
4. **Confirmation record.** A durable, out-of-band record (outside the
   deleted workspace's own tables, obviously) that a deletion request was
   received, scoped, and completed/partially-completed-with-named-
   exceptions — itself subject to a retention decision (how long does
   the record that "we deleted X" need to survive?).

## 4. Legal-hold marker — design sketch (not built)

A legal hold must suspend retention-driven deletion for a named scope
(a workspace, a specific investigation's row set, or a whole table)
without requiring every retention mechanism above to be individually
hold-aware.

**Sketch**: a single `legal_holds` table (`id`, `scope_type` enum
`{WORKSPACE, TABLE, ROW_SET}`, `scope_ref` (workspace_id / table name /
an explicit id list), `reason` text, `placed_by`, `placed_at`,
`released_at` nullable, `released_by` nullable). Every retention path
this document's §2 inventory names — the partition-drop loop in
`maintenance/retention.py::run_retention`, and any future age-based
`DELETE` mechanism for §2.1/2.5/2.6.c-shaped tables — consults this table
before acting: `run_retention` would gate a partition drop on "no active
`WORKSPACE`-scope hold exists for any workspace with rows in this
partition's date range" (a coverage check structurally identical in
shape to the existing `rollups_cover` verify-before-drop gate §2.3
already uses, and cite-worthy as the precedent for how this codebase
already expresses "check before an irreversible drop"). **Not built** —
this is a design sketch for the owner to size against real legal-hold
frequency before committing engineering time; PENDING OWNER/COUNSEL
whether legal holds are even expected to be common enough to justify
this machinery vs. a manual pause-the-cron-job procedure.

## 5. Deletion-propagation checklist

When any deletion (retention-driven, tenant-requested, or legal-hold
release) actually runs, the following must all be true for it to count as
complete — a checklist an implementer or auditor can walk, not prose to
re-derive each time:

- [ ] **Primary table row(s) removed** (or partition dropped) per §2's
      mechanism for that data class.
- [ ] **Evidence blob resolved and removed**, if `offer_raw_evidence_hash`
      pointed at one (§2.1) — deleting the DB row without the blob leaves
      an orphaned, undeletable-by-reference file; deleting the blob
      without checking no OTHER row still references the same
      content-addressed hash (two observations can hash to identical
      bytes) corrupts a still-live reference. Content-addressing makes
      this a **reference-count check**, not a 1:1 cascade.
- [ ] **Rollup/derived rows re-derived or explicitly retained as
      historical aggregate** — a `variant_price_daily_rollups` row is
      already a step removed from the raw data it aggregates (§2.3's
      `rollups_cover` gate exists precisely so a rollup survives its raw
      source); a tenant-deletion flow must decide explicitly whether the
      rollup itself is in scope, not assume the raw-row deletion silently
      handles it (it does not — they are different tables, deleted
      independently).
- [ ] **Cache/read-model invalidation** — `variant_price_states` (SPEC-09
      current-state surface) is a derived cache the deleted data fed;
      confirm it no longer reflects the deleted workspace/rows.
- [ ] **Backup propagation is out of scope for individual-row deletion**
      — §2.7's backups are full-database dumps on a 4-hour cadence;
      "delete tenant X's data" does not retroactively edit a completed
      backup set. **PENDING OWNER/COUNSEL**: does a completed deletion
      obligation require purging the tenant's data from ALREADY-TAKEN
      backup sets too (a much harder problem — logical dumps are not
      designed for surgical row removal), or does the backup's own
      retention window (§2.7, itself unresolved) satisfy the obligation
      once it naturally ages out? This is a real, unresolved question
      this document deliberately does not answer.
- [ ] **Legal hold checked** (§4) before the deletion runs, not after.
- [ ] **Confirmation record written** (§3 step 4) if this was a
      tenant-initiated deletion.
- [ ] **Audit trail of the deletion itself retained** per §2.5's audit-row
      posture — a deletion is itself an event worth an audit row, subject
      to its OWN retention question (do not let "delete everything" also
      delete the record that a deletion happened).

---

## 6. Cross-references

* `libs/shared/app_shared/observations/EVIDENCE_RETENTION.md` — evidence-
  blob policy (authoritative for §2.1; extended, not duplicated, here).
* `libs/shared/app_shared/maintenance/registry.py` — the code-level
  constant naming which tables are partition-managed and by which
  `Settings` attribute (§2.2-2.4).
* `libs/shared/app_shared/maintenance/retention.py` — the actual
  partition-drop + rollup-table-age-delete mechanism.
* `scripts/dr/RUNBOOK.md` — the existing, honest backup RPO/RTO and
  capacity-floor statement (§2.7).
* `libs/shared/app_shared/models/network_operations.py` — the
  trigger-enforced append-only billing ledger (§2.6.a).
* `READY_REGISTER.md` (`/srv/crawmatic/evidence/READY_REGISTER.md`) —
  READY-013-h names the rollup-watermark backlog that currently blocks
  `price_observations` retention in practice (§2.3).
* `docs/INCIDENT_RESPONSE.md` (this same task, item D) — a legal hold or
  a deletion-propagation failure is itself a candidate incident under
  that document's severity ladder.

---

## 7. What this document is not

Not a data-processing agreement, not a privacy policy, not a DPIA, and
not a substitute for counsel review of the jurisdictions this system
actually operates in. It is the inventory and mechanism map counsel needs
to turn PENDING into a number.
