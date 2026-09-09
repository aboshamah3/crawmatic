# Data Retention Policy — Program Document

EPA task W5.5-GA, item C (report §10); **durations decided by EPA C9
(F14), owner decision 11, 2026-09-08.** Every duration and
irreversible-action threshold below now carries a DECIDED DEFAULT — a
concrete number that exists in `libs/shared/app_shared/config.py` and
that the retention code will use — followed by a literal
"Ratified by owner on ____" line for the owner to date and sign.

**Nothing is removed until that line is signed and the class is
named.** `Settings.RETENTION_ENABLED_CLASSES` ships EMPTY; every
retention family in `app_shared.maintenance.registry.RETENTION_FAMILIES`
is checked against it before any drop, row sweep or summarisation
(`retention_class_enabled`). A default in this document is therefore a
PROPOSAL the code is ready to execute, not a policy in force — see §2.9.

A decided default is an operational and financial-record judgement, not
a legal opinion: counsel has NOT reviewed these numbers, and the "Legal
basis" rows below say so explicitly rather than implying otherwise. What IS
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

* **Retention duration** — the DECIDED DEFAULT (a concrete number, with
  the `Settings` field that holds it), followed by
  **Ratified by owner on ____**. The number is what the code will do once the class is
  enabled; the signature line is what authorises enabling it. Until the
  line is dated, the class stays out of
  `Settings.RETENTION_ENABLED_CLASSES` and the mechanism does nothing.
* **Legal basis** — decided default: **none asserted.** The durations
  here are set on operational and financial-record grounds only, and no
  claim of GDPR/PDPL sufficiency is made by any of them. **Ratified by owner on ____**.
  This system
  operates across jurisdictions (GCC-region merchants per
  `saas-wt-w51`'s Arabic/RTL/GCC work, `specs/018-arabic-rtl-gcc/`) whose
  data-retention and deletion-obligation regimes this document does not
  attempt to resolve. Do not treat any duration below as GDPR/PDPL-
  compliant until counsel confirms it — signing the ratification line
  authorises the ENGINEERING behaviour, and is not a legal finding.
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
| **Current mechanism** | `evidence_store.py`'s `store_evidence`/`resolve_hash`/`replay` (write-once, read-many, integrity-checked on every read). **GAP CLOSED, EPA C5 2026-09-08.** `evidence_store.py`'s `sweep_evidence_retention`, run daily as `MAINTENANCE_EVIDENCE_RETENTION` (`app_shared.task_names`, scheduled via `CADENCE_EVIDENCE_RETENTION`), deletes blobs past `EVIDENCE_RETENTION_DAYS` — but ONLY those no observation inside `RETENTION_PRICE_OBSERVATIONS_DAYS` still references, which is exactly the gate the "Deletion mechanism" row below prescribed. A database error propagates rather than being read as "nothing is referenced": a sweep that cannot prove a blob is unreferenced deletes nothing. The duration is now a decided default (30 days) awaiting the ratification line below. |
| **Retention duration** | **Decided default: 30 days** (`Settings.EVIDENCE_RETENTION_DAYS`). Shortest window in this document on purpose: the blob is a means of re-deriving an observation, and the observation itself (§2.3, 180 days) is the evidence of record. **Ratified by owner on ____**. Production placement is `Settings.EVIDENCE_STORE_DIR`, a Railway volume mounted on both scraper services; unset means no blob is written and no hash is recorded at all — deliberately, because a dangling content address is worse than a NULL column. |
| **Legal basis** | Decided default: none asserted — operational grounds only. **Ratified by owner on ____**. |
| **Deletion mechanism** | Shipped (EPA C5): filesystem object deletion keyed by hash, gated on no `price_observations.offer_raw_evidence_hash` row still referencing it (a hash referenced by a NOT-yet-retention-eligible observation must not be deleted out from under it — see §2.2's partition-drop-vs-rollup-coverage precedent for the shape this gate should take). |
| **Append-only?** | Write-once by convention (`evidence_store.py`), not DB-trigger-enforced — it is a filesystem store, not a table. |

### 2.2 `request_attempts` + partitions

| | |
|---|---|
| **What** | Per-fetch-attempt record (byte accounting, success/failure, origin). Monthly-partitioned by `created_at`. |
| **Current mechanism** | `libs/shared/app_shared/maintenance/registry.py`'s `PARTITIONED_TABLES` registers this table with `retention_setting="RETENTION_REQUEST_ATTEMPTS_DAYS"`, `feeds_rollups=False` (drops by age alone, FR-019). `libs/shared/app_shared/maintenance/retention.py::run_retention` does the actual `DROP TABLE IF EXISTS` per eligible whole partition (`partition_eligible`, FR-018 — a partition is eligible only when its **entire** range is past the cutoff). |
| **Retention duration** | **Decided default: 90 days** (`Settings.RETENTION_REQUEST_ATTEMPTS_DAYS`). Operational telemetry whose durable outcome already lives in `price_observations`/`match_current_prices`; 90 days is one quarter of trend, which is what anyone actually asks of it. Retention class `request_attempts`. **Ratified by owner on ____**. |
| **Legal basis** | Decided default: none asserted — operational grounds only. **Ratified by owner on ____**. |
| **Deletion mechanism** | Whole-partition `DROP TABLE` — never a bulk `DELETE` (SC-003, `retention.py`'s own docstring: *"never a bulk DELETE on a raw append-heavy table"*). |
| **Append-only?** | Partitioned + high-volume ("raw append-heavy" per the module's own docstring) but **not** trigger-enforced append-only — no `CREATE TRIGGER ... append_only` found on this table (contrast §2.5). Deletion is still partition-drop-only by *design choice* (performance/lock-avoidance), not by structural enforcement. |

### 2.3 Price observations

| | |
|---|---|
| **What** | `price_observations` — immutable extraction-attempt result (price, currency, comparability, plus the W3.1 `offer_*` superset). Monthly-partitioned by `scraped_at`. |
| **Current mechanism** | Same registry (`PARTITIONED_TABLES`), but `feeds_rollups=True` — the ONE entry requiring `rollups_cover` (`retention.py::rollups_cover`) to pass before a partition drops. Since EPA C8 (F13) this is TWO gates that must BOTH hold, either failing retains the partition and reports `partitions_skipped_pending_rollups` (self-healing — re-checked next run, never silently dropped, research R7): **(1) per-key coverage** — every `(workspace_id, product_variant_id, date)` key with a source observation in the partition's range must already have a covering `variant_price_daily_rollups` row (a date-only match, the pre-C8 gate, could pass while one tenant's/variant's rollup was silently missing as long as *some* other rollup landed that day); **(2) the completion watermark** — every UTC calendar date in the partition's range must have a `rollup_completion` row (C7's checkpoint table) with `complete = true`, i.e. the day's keyset rollup batch loop ran to the end rather than being killed partway (a partial day can already have per-key-correct rows for the pairs it reached, which gate 1 alone cannot distinguish from a finished day). Failed and no-price-competitor observations are never a reason to retain or silently drop a partition: a variant with zero comparable competitors that day still gets a `variant_price_daily_rollups` row (`comparable_competitor_count = 0`, `average/cheapest/highest_competitor_price = NULL`) — the absence of a *usable* competitor price is represented as data in the rollup, not as a missing rollup row, so it satisfies gate 1 exactly like any other covered key. |
| **Retention duration** | **Decided default: 180 days** (`Settings.RETENTION_PRICE_OBSERVATIONS_DAYS`, raised from 90 by EPA C9). Doubled deliberately: this is the row a pricing decision traces back to, and a 90-day window put the evidence BELOW any realistic dispute horizon — the aggregate that outlives it (`variant_price_daily_rollups`, 730 days) cannot answer "which page said what, when". Retention class `price_observations`. **Ratified by owner on ____**. |
| **Legal basis** | Decided default: none asserted. Flagged as the row most likely to acquire one: a dispute/audit-trail obligation would bind HERE, not on the rollups. **Ratified by owner on ____**. |
| **Deletion mechanism** | Whole-partition `DROP TABLE`, verify-before-drop gated on rollup coverage. |
| **Append-only?** | No DB trigger found. Partitioned + append-heavy by convention, same posture as §2.2. |
| **Known coupling** | READY-013-h (`READY_REGISTER.md`) names a **known Jul-11→Aug-12 rollup backlog that never self-heals today**, which "keeps the retention safety-hold shut" — i.e. this table's retention is currently *blocked in practice* by an unrelated watermark bug, not by policy. That is a separate GA item (READY-013-h, not this task) but is recorded here because it directly gates whether `price_observations` retention can ever actually run. |

### 2.4 Alerts (`price_alert_events`)

| | |
|---|---|
| **What** | Alert-event log, partitioned by `created_at`. |
| **Current mechanism** | `PARTITIONED_TABLES` entry, `feeds_rollups=False`, `retention_setting="RETENTION_PRICE_ALERT_EVENTS_DAYS"`. Same whole-partition-drop mechanism as §2.2. |
| **Retention duration** | **Decided default: 365 days** (`Settings.RETENTION_PRICE_ALERT_EVENTS_DAYS`). A year, because an alert is the customer-visible artefact of the product — "why did I get this alert last spring" is a support question with a one-year natural period. Retention class `price_alert_events`. **Ratified by owner on ____**. |
| **Legal basis** | Decided default: none asserted — operational grounds only. **Ratified by owner on ____**. |
| **Deletion mechanism** | Whole-partition `DROP TABLE`. |
| **Append-only?** | No DB trigger found; convention only. |
| **Also registered, not yet live** | `webhook_events` (SPEC-16) is registered in `PARTITIONED_TABLES` ahead of its own migration and is currently absent from the schema — `table_exists` skips it cleanly (FR-002) until it lands. Listed here so the retention program covers it the moment it ships, rather than the registry silently getting ahead of a policy decision. **Decided default: 90 days** (`Settings.RETENTION_WEBHOOK_EVENTS_DAYS`), retention class `webhook_events`. **Ratified by owner on ____**. |

### 2.5 Audit rows

#### 2.5.a `domain_lifecycle_audit`

| | |
|---|---|
| **What** | Domain-lifecycle transition audit trail (`libs/shared/app_shared/models/domain_playbooks.py::DomainLifecycleAudit`, migration `9f24d748ba13_domain_lifecycle_transitions_and_audit.py`). |
| **Current mechanism** | Not registered in `PARTITIONED_TABLES` — **no automated retention exists for this table today.** Not partitioned. |
| **Retention duration** | **Decided default: 730 days, NOT YET IMPLEMENTED.** An audit trail's window is driven by the *longest* applicable obligation, never by storage cost, so it takes the longest number in this document by analogy to the ledger (§2.6.a) rather than to the raw tables above. No retention family is registered for it — the number is a decision recorded ahead of a mechanism, deliberately, so whoever builds one is not re-deciding it. **Ratified by owner on ____**. |
| **Legal basis** | Decided default: none asserted; the most likely of all classes here to acquire one. **Ratified by owner on ____**. |
| **Deletion mechanism** | None shipped. `domains/` and `scripts/domain_lifecycle.py` are fenced for this task (another worker's active lane) — this document names the gap; it does not propose a mechanism inside the fenced module. |
| **Append-only?** | App-level audit-log convention (a lifecycle transition is inherently a historical fact), **not** DB-trigger-enforced — no `CREATE TRIGGER` found on this table. |

#### 2.5.b `strategy_method_switches`

| | |
|---|---|
| **What** | Strategy-chain switch audit (`libs/shared/app_shared/models/strategy_switches.py::StrategyMethodSwitch`, migration `05dfda7cdbb7_rollup_watermark_and_strategy_switch_audit.py`). |
| **Current mechanism** | Not registered in `PARTITIONED_TABLES` — no automated retention. `strategy/` is fenced for this task. |
| **Retention duration / legal basis / deletion mechanism** | **Decided default: 730 days** / none asserted / none shipped — same posture and same reasoning as 2.5.a. **Ratified by owner on ____**. |
| **Append-only?** | App-level convention only. |

#### 2.5.c `match_audit_classifications`

| | |
|---|---|
| **What** | Match-identity classification history (`libs/shared/app_shared/models/match_audit.py`, EPA A6). |
| **Current mechanism** | App-enforced append-only invariant per the module's own comment (*"a new classification is always an [insert]"*) reinforced by a partial unique index (`uq_mac_match_id_current`) enforcing "at most one CURRENT classification per match" **in the database** — a real constraint, though the append-only property itself (no row is ever mutated once superseded) is an application convention around that index, not a `BEFORE UPDATE`/`BEFORE DELETE` trigger like §2.6 below. |
| **Retention duration / legal basis / deletion mechanism** | **Decided default: 730 days** / none asserted / none shipped — audit-row posture, as 2.5.a. **Ratified by owner on ____**. |

### 2.6 Billing / ledger rows

This is the class where "append-only" stops being a convention and starts being a **database-enforced structural property** — retention here cannot be "delete old rows," full stop, without an owner-authorized exception path.

#### 2.6.a `network_operations` family (`network_operations`, `network_operation_allocations`, `network_operation_settlements`)

| | |
|---|---|
| **What** | The physical cost ledger (C1/C4/C5) — fleet-owned (no `workspace_id`), the durable record of physical resource consumption independent of any provider dashboard. |
| **Append-only?** | **TRUE, DB-trigger-enforced.** `libs/shared/app_shared/models/network_operations.py`: a closed `network_operations` row is protected by a `BEFORE UPDATE` trigger that rejects any update once `closed_at` is non-NULL (*"A closed physical operation is an append-only fact"*); `network_operation_settlements` has `trg_network_operation_settlements_append_only` explicitly rejecting **both** UPDATE and DELETE (*"network_operation_settlements is append-only"*, the model docstring: *"UPDATE and DELETE are both rejected by a trigger, so 'append-only' [...] is structural, not a convention"*). An allocation-total constraint trigger additionally re-checks correctness at COMMIT. |
| **Deletion mechanism** | **None can exist as a row `DELETE` against a live, closed operation — the trigger will reject it.** The only lawful paths are: (a) an **owner-authorized exception** that explicitly disables/bypasses the trigger for a scoped, logged, reversible-audit operation (not built — **decided default: this exception path is NOT built and is not planned**, because it directly undermines the guarantee the trigger provides and no requirement has been produced that needs it; **Ratified by owner on ____**); or (b) **path (b) is now the real one.** `network_operations` IS monthly-partitioned as of EPA C9 (`alembic/versions/a5e0c74b13d9_partition_network_operations.py`) and IS registered in `PARTITIONED_TABLES`, so whole-partition drop is its retention mechanism — no trigger bypass required, because dropping a partition is a DDL operation the row-level trigger never sees. A third mechanism now exists for the ledger's highest-volume subset: `app_shared.maintenance.ledger_summaries` compresses a settled navigation's subresource children into one `network_operation_resource_summaries` row and removes them in the same transaction (see §2.9). |
| **Retention duration / legal basis** | **Decided defaults: 730 days for `network_operations` parents (`RETENTION_NETWORK_OPERATIONS_DAYS`, class `network_operations`), 730 days for `network_operation_allocations` (`RETENTION_COST_ALLOCATIONS_DAYS`, class `cost_allocations`), 30 days for browser SUBRESOURCE children (`RETENTION_NETWORK_OPERATION_CHILDREN_DAYS`, class `network_operation_children`) — and children are SUMMARISED, not merely removed.** Two years is the financial-record analogy, chosen because this is billing/cost evidence and the longest applicable obligation governs. The allocation window is deliberately EQUAL to the parent's: an allocation outliving its operation is an orphan, and an operation outliving its allocations is a cost nobody owns; if a jurisdiction ever forces them apart, the allocation window must be the SHORTER one. Legal basis: none asserted — this is the class most likely to acquire a real one (financial-record retention law is commonly multi-year and jurisdiction-dependent), and counsel should review it before the class is enabled, not after. **Ratified by owner on ____**. |

#### 2.6.b `provider_usage_records`

| | |
|---|---|
| **What** | Immutable, content-addressed (`sha256:` over exact export bytes) provider-reported usage evidence (C5, `libs/shared/app_shared/models/provider_usage.py::ProviderUsageRecord`, migration `7c2b9e5a41d6`). Fleet-owned. Re-importing the same file is a no-op; a genuinely different file always lands as new rows — never merged or summarized away (`netledger/reconcile.py`'s module docstring). |
| **Append-only?** | Evidence rows are never merged/mutated by design (app convention — a re-import of different bytes is new rows, not an update to old ones); no DB trigger confirmed on this specific table in the review for this task (distinct from §2.6.a's `network_operations` family, which IS trigger-enforced — do not conflate the two). |
| **Retention duration / legal basis / deletion mechanism** | **Decided default: 730 days** / none asserted / none shipped — same financial-record reasoning as §2.6.a, and deliberately the same number: this is the evidence a settlement in §2.6.a was derived FROM, so it must never expire before what it justifies. No retention family is registered for it yet. **Ratified by owner on ____**. |

#### 2.6.c `cost_reservations` / `cost_budgets` / `fleet_cost_budgets` (C3)

| | |
|---|---|
| **What** | The cost-authorization store — reservations, budgets, entitlement evidence (`libs/shared/app_shared/costauth/`, `libs/shared/app_shared/models/cost_authorization.py`, migration `f1a7c02de5b4`). `costauth/` is **fenced** for this task (fresh W5.5-L1 work) — this document cites it for the retention inventory only, does not read deep into its logic beyond what's needed to classify the data. |
| **Append-only?** | **NOT append-only** — this is live operational state (a reservation transitions RESERVED -> settled/released; `service.py`: *"Settlement and release are compare-and-set on the reservation's own [state]"*). A TERMINAL (settled/released) reservation is the historical fact worth retaining; the mutable pre-terminal state is not itself audit evidence in the same sense as §2.6.a. |
| **Retention duration / legal basis / deletion mechanism** | **Decided default: 30 days for `cost_reservations`** (`RETENTION_COSTAUTH_RESERVATIONS_DAYS`, class `costauth_reservations`) / none asserted / **shipped** (EPA C9). Short, because a reservation is a LEASE, not a record: the money fact it produced lives on `cost_budgets` and in the ledger, and a settled lease answers nothing 30 days later. As this row predicted, the mechanism is the structurally simple one — a bounded, batched age-based sweep restricted to `state IN ('SETTLED','RELEASED')` (`app_shared.maintenance.retention._row_delete_stmt`), needing no trigger bypass. **A `RESERVED` row is never touched at any age**: it is holding real money against a budget, and removing it would silently return funds the fleet has already committed. `cost_budgets`/`fleet_cost_budgets` are current-state counters with no retention family at all — they are not history. **Ratified by owner on ____**. |

### 2.7 Backups (incl. the W5.4 DR artifacts)

| | |
|---|---|
| **What** | Encrypted logical dumps of both prod DBs (`/srv/crawmatic/backups/dr/sets/set-<UTC>/{engine,saas}.dump.gpg` + manifest + `SHA256SUMS`), produced every 4h by `scripts/dr/backup_prod.sh`; restore-verified daily by `scripts/dr/verify_restore.sh` (`scripts/dr/RUNBOOK.md`, `scripts/dr/dr_lib.sh`). A second, independent, **unencrypted, non-restore-verified** nightly SaaS-only backup also exists (`/etc/cron.d/crawmatic-saas-backup` -> `saas/deploy/railway/backup-prod-db.sh`, writing to `/srv/crawmatic/backups/saas/`) — the DR runbook explicitly names `backups/dr/` as "the recovery point of record," not this second path. |
| **Current mechanism / retention budget** | `scripts/dr/RUNBOOK.md` §2 is the existing, ALREADY-HONEST retention/capacity statement for this class — cited here, not duplicated: RPO is **4 hours** (cron cadence), explicitly **not** the owner-approved 15-minute objective (*"NOT met by this automation"*), because (a) each cycle is a full logical dump (~127MB engine + ~33MB SaaS, ~22MB compressed combined) taking ~50s, and (b) the ops host was at **~91% disk with ~7GB free** at the time of writing, and the backup scripts **refuse to run below 2GB free** — a self-imposed retention/capacity floor already enforced in code, cited here rather than restated. RTO is met with roughly two orders of magnitude of headroom (measured: ~66s full backup-restore-assert cycle vs. a 2h objective) — see the runbook for the caveat that this measures data-restore only, not full service reprovisioning. |
| **Retention duration (how many historical backup SETS are kept, not RPO)** | **Not found in the reviewed scripts for this task** — `scripts/dr/dr_lib.sh` is named in the runbook as owning "retention" logic, but this task did not re-derive its exact sweep window (out of scope to re-audit W5.4's own implementation; cite the file, don't re-verify its number). **Decided default: 30 days of backup SETS**, subject to the 2GB-free floor winning wherever the two conflict — a retention window that doesn't respect that floor is a self-inflicted outage, not a policy choice, per the runbook's own reasoning. The owner must confirm `scripts/dr/dr_lib.sh`'s actual configured sweep window matches this number before signing; this document records the decision, it does not silently change that script. **Ratified by owner on ____**. |
| **Legal basis** | Decided default: none asserted — operational grounds only. **Ratified by owner on ____**. |
| **Deletion mechanism** | `dr_lib.sh`'s retention sweep (cited, not re-verified — see above). |
| **Append-only?** | N/A — these are file-system artifacts, not DB rows. |

### 2.8 Logs

| | |
|---|---|
| **What** | Application/service logs (API, worker, scheduler, scraper processes) and CI/build logs. |
| **Current mechanism** | **No log-retention mechanism was found anywhere in this codebase during this task's review** (`LOG_RETENTION`, `logrotate`, and similar patterns returned zero matches across `.py`/`.md`/`.sh`). Log lifecycle today is whatever the deployment platform (Railway) and any ambient host `logrotate` provide — neither is a policy this repository controls or has documented. |
| **Retention duration / legal basis / deletion mechanism** | **Decided default: 30 days** / none asserted / **none shipped — this remains a real gap.** Thirty days is the shortest window in this document on purpose: logs commonly carry PII (request paths, error messages echoing user input), nothing in this repository controls their lifecycle today, and the honest posture for uncontrolled PII is to keep it briefly. Recording the number does not implement it — the platform (Railway) still decides, and closing that is a GA item in its own right, not a formality. **Ratified by owner on ____**. |

### 2.9 Operational state (EPA C9, F14) — and the ratification switch

Three classes that were not inventoried before this task, plus the
mechanism that governs every class in this document.

#### 2.9.a `scrape_job_targets` / `dispatch_intents`

| | |
|---|---|
| **What** | Per-target execution state (`scrape_job_targets`) and the commit-before-send dispatch record (`dispatch_intents`). Neither is partitioned. |
| **Retention duration** | **Decided defaults: 90 days** (`RETENTION_SCRAPE_JOB_TARGETS_DAYS`, class `scrape_job_targets`) and **30 days** (`RETENTION_DISPATCH_INTENTS_DAYS`, class `dispatch_intents`). The intent's window is short because its own reconcile deadline is five MINUTES (`DISPATCH_RECONCILE_INTERVAL_SECONDS`) — a settled intent 30 days later answers no question anyone can still ask. **Ratified by owner on ____**. |
| **Legal basis** | Decided default: none asserted — operational grounds only. **Ratified by owner on ____**. |
| **Deletion mechanism** | Bounded, batched age sweep restricted to TERMINAL rows (`retention.py::_row_delete_stmt`). Non-terminal rows are never touched at any age: a `PENDING`/`STARTED`/`DEFERRED` target is live work (`DEFERRED` reads like an ending and explicitly is not one — it returns to `STARTED` on re-pickup), and a `POSTED` intent is the one genuinely ambiguous state the row exists to record. |
| **Append-only?** | No — both mutate through their lifecycle. |

#### 2.9.b Browser subresource children — summarised, not merely removed

| | |
|---|---|
| **What** | `network_operations` rows carrying a `parent_operation_id`: one per subresource a browser navigation pulled in. A subset of a table, not a table — which is why the retention registry is keyed by CLASS rather than by table name. |
| **Retention duration** | **Decided default: 30 days** (`RETENTION_NETWORK_OPERATION_CHILDREN_DAYS`, class `network_operation_children`). **Ratified by owner on ____**. |
| **Deletion mechanism** | `app_shared.maintenance.ledger_summaries`, task `MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN`, daily. For each parent older than the window **whose provider settlement has ARRIVED**, it writes one `network_operation_resource_summaries` row (child count, a per-host-class byte map, summed duration) and removes the children **in the same transaction**. The parent's own totals are untouched — they are immutable-by-trigger facts about the navigation itself. |
| **Why the settlement gate** | Reconciliation apportions a period-billed provider charge across operations **by their transport-observed bytes**, and a navigation's bytes are its children's bytes. Summarising an unsettled parent would remove the rows a not-yet-arrived invoice will be reconciled against, and the resulting settlement would be silently wrong rather than loudly impossible. An unsettled parent therefore keeps every child, however old, and is counted in `parents_deferred_unsettled` so "nothing was summarised" is legible instead of mysterious. |
| **What was lost to partitioning** | Partitioning `network_operations` cost the ledger five foreign keys (both self-references, plus `network_operation_allocations.operation_id`, `network_operation_settlements.operation_id` and `request_attempts.network_operation_id`): a foreign key must reference a UNIQUE constraint, and a partitioned table's unique constraint must include the partition key. `network_request_id` is consequently unique **per month**, not globally — it is a caller-minted UUIDv7, so a collision is not a thing that happens, but it is no longer a thing the database forbids. `ledger_summaries.find_orphan_references` is the scheduled stand-in: it REPORTS unresolved references and never repairs them, because some orphans are correct (a referencing row naturally outlives an operation whose partition has been dropped) and a job that removed them would be destroying the newest evidence to tidy up after the oldest. **This trade should be ratified, not merely deployed.** **Ratified by owner on ____**. |

#### 2.9.c The ratification switch — how a number in this document becomes a policy in force

Every mechanism named anywhere above is gated on ONE setting:

```
RETENTION_ENABLED_CLASSES = []          # ships empty; nothing is removed
RETENTION_ENABLED_CLASSES = "price_observations,request_attempts"
RETENTION_ENABLED_CLASSES = "*"         # every registered class
```

`app_shared.maintenance.registry.retention_class_enabled` is consulted
by `run_retention` (before the table is even probed) and by
`run_ledger_child_summarization` (before it looks at any data). An
unknown class name raises at `Settings` construction, so a typo fails
the deploy instead of quietly leaving a family disabled that the owner
believes they enabled.

The enabling procedure, in order:

1. Sign and date the **Ratified by owner on ____** line for the class in the section above.
2. Add that class's `class_key` to `RETENTION_ENABLED_CLASSES`.
3. Deploy. The next `MAINTENANCE_RETENTION_DROP` /
   `MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN` tick acts on it.

Step 2 without step 1 is the failure this switch exists to prevent, and
the only thing standing in its way is that the two are separate steps.

| Class key | Window | Mechanism | Ratified |
|---|---|---|---|
| `price_observations` | 180 d | partition drop, rollup-coverage gated (§2.3) | ____ |
| `request_attempts` | 90 d | partition drop | ____ |
| `price_alert_events` | 365 d | partition drop | ____ |
| `webhook_events` | 90 d | partition drop (table not yet live) | ____ |
| `network_operations` | 730 d | partition drop (new, EPA C9) | ____ |
| `network_operation_children` | 30 d | summarise-then-remove (§2.9.b) | ____ |
| `cost_allocations` | 730 d | grouped row sweep (§2.6.a) | ____ |
| `variant_price_daily_rollups` | 730 d | age-based row sweep | ____ |
| `scrape_job_targets` | 90 d | terminal-state row sweep | ____ |
| `dispatch_intents` | 30 d | terminal-state row sweep | ____ |
| `costauth_reservations` | 30 d | terminal-state row sweep | ____ |

#### 2.9.d The privilege that made all of this inert (EPA C9, F14)

Worth recording because it was invisible for months and would have made
every number above meaningless. `scripts/provision_db_roles.sql` §8
moves ownership of every table to `crawmatic_migrate` — a role no
service logs in as — and states that "the maintenance job's partition
creation is granted explicitly rather than by making a runtime role the
owner". It then granted no such thing. PostgreSQL requires OWNERSHIP of
the parent to create or reclaim a partition (there is no grantable
"attach a partition" privilege), so on a correctly provisioned database
`crawmatic_auth` — the role `MAINTENANCE_PARTITION_CREATE` and
`MAINTENANCE_RETENTION_DROP` actually run as — could do neither.
Partition creation and retention were both broken by design, silently.

§9 of that script is the fix: two SECURITY DEFINER functions owned by
the table owner, EXECUTE granted only to `crawmatic_auth`, each
validating its argument against `pg_catalog` so neither is a general DDL
hole. Creation additionally copies the parent's grants and RLS posture
onto the new child inside the same privileged call — a partition does
not inherit either, and `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` is
itself an owner-only operation, so the child has to be born isolated
rather than repaired afterwards.
`app_shared.maintenance.partitions` probes for the two functions and
routes through them when present, falling back to direct DDL when not,
so a database provisioned before §9 behaves exactly as it did.

**This is a deploy-order dependency, not just a bug fix:** enabling any
partition-drop class above requires the target database to have been
re-provisioned with §9 present. Without it, retention will report
nothing dropped and log a privilege error — fail-closed, but not for the
reason an operator would guess.

Classes with a decided default but NO mechanism yet — evidence blobs
(§2.1, its own task), the audit tables (§2.5), `provider_usage_records`
(§2.6.b), backups (§2.7) and logs (§2.8) — are deliberately absent from
this table: the switch cannot enable what nothing implements.

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
   design forbids row-level removal," and its **decided default is
   (a): wait for the natural age-out.** A tenant-deletion obligation is
   satisfied by the partition's own window — 180 days for observations,
   90 for attempts — and the confirmation record in step 4 states that
   date explicitly rather than claiming immediacy the design cannot
   deliver. Choosing (b) would mean building the one thing every module
   docstring in this system refuses to build. **Ratified by owner on ____**; and an **owner-authorized
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
frequency before committing engineering time; the open question is
whether legal holds are even expected to be common enough to justify
this machinery vs. a manual pause-the-cron-job procedure. **Decided
default: the manual procedure, for now** — and it is now a real one
rather than a hope: removing a class from
`Settings.RETENTION_ENABLED_CLASSES` and redeploying stops that class's
retention entirely, with no code change and no cron surgery, which is
exactly the pause a legal hold needs at the scale this system is at.
The `legal_holds` table above becomes worth building when holds are
frequent enough that a per-workspace hold is needed rather than a
per-class one. **Ratified by owner on ____**.

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
      backup set. **Decided default: the backup's own retention window
      satisfies the obligation once it naturally ages out** (§2.7, 30
      days of sets) — purging a tenant from already-taken logical dumps
      is not a thing logical dumps support, and a policy that requires
      it would be a policy this system cannot keep. The 30-day backup
      window is therefore also the maximum lag between a completed
      deletion and the last copy of the deleted data disappearing, and
      the confirmation record (§3 step 4) should say so. **Ratified by owner on ____**.
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
