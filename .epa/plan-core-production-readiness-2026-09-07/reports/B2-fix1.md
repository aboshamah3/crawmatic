# Task Report: B2-fix1 — Fix B10's two release-blocking Stage B bugs at source
Status: DONE
Attempt: 1

Files changed:
- `alembic/versions/b6e5d1c94a72_dispatch_intent_node_and_state.py` — AMENDED IN PLACE (no new
  revision): the `scrapyd_job_id` backfill moved out of a standalone `UPDATE` and into the `USING`
  expression of the `ALTER COLUMN ... TYPE uuid` rewrite, which RLS cannot filter. Docstring
  section rewritten to explain why (and why a `SELECT` self-check would have been useless).
- `tests/unit/test_migration_offline_dispatch_intent_backfill.py` — NEW (5 tests): renders the
  revision offline and fails if it ever emits DML against `dispatch_intents` again, or if the
  `USING` expression loses either backfill branch. Confirmed RED against the old migration text.
- `scripts/provision_db_roles.sql` — the three Stage-B tables (`domain_rules`,
  `refresh_rule_occurrences`, `strategy_discovery_state`) added to the grant loop matching each
  one's reviewed privilege set, for all three roles; new header note stating the arrays are a
  derived artifact and naming the test that enforces it.
- `tests/unit/test_grants_manifest_schema.py` — NEW section (6 tests): parses
  `provision_db_roles.sql`'s `ARRAY[...]` literals and asserts, per role, that the table set AND
  the applied privileges match `scripts/sql/grants_expected.yaml` exactly. Confirmed RED against
  the pre-fix arrays.
- `docs/ops/RELEASE_2_2026-09.md` — the mandatory pre-migrate `UPDATE` and post-migrate `GRANT`
  blocks demoted to read-only VERIFICATION queries; §2.6 heading, §2.7's "step 0", §3 sign-off and
  owner-item 1 updated; both findings marked FIXED AT SOURCE; B10's rehearsal evidence kept
  verbatim and a new "Re-rehearsal after the source fixes — 10/10 GREEN" subsection added.

Intended commit message (NOT committed — orchestrator commits per phase):
`fix(db): backfill dispatch_intents inside the type rewrite (RLS-proof) + provision the 3 Stage B tables (B2-fix1)`

## Verification
1. **Unit gate (required)** — the suite exceeds the 600 s foreground cap, so it was run in three
   disjoint chunks covering every file under `tests/unit` (303 top-level files + 7 package dirs);
   totals reconcile exactly with B10's 4415/19/6 baseline plus this task's 11 new tests.
   `sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | head -152) -q -p no:cacheprovider -m "not integration"` → exit 0
   ```
   1889 passed, 1 skipped, 18 warnings in 363.86s (0:06:03)
   ```
   `sudo -u mahmoud .venv/bin/pytest $(ls tests/unit/test_*.py | tail -151) -q -p no:cacheprovider -m "not integration"` → exit 0
   ```
   2261 passed, 2 deselected, 53 warnings in 294.14s (0:04:54)
   ```
   `sudo -u mahmoud .venv/bin/pytest tests/unit/{observations,models,scrapyd,netledger,strategy,domains,jobs} -q -p no:cacheprovider -m "not integration"` → exit 0
   ```
   276 passed, 18 skipped, 4 deselected, 3 warnings in 4.79s
   ```
   Sum: **4426 passed, 19 skipped, 6 deselected** = B10's 4415 + 11 new tests; skips/deselects
   unchanged. No regression.

2. **Single Alembic head unchanged** — `sudo -u mahmoud bash scripts/check_single_head.sh` → exit 0
   ```
   check_single_head: OK — exactly 1 head.
   e7b34c0af219 (head)
   ```
   (`b6e5d1c94a72` was amended in place; no revision added, chain and head identical.)

3. **B10's rehearsal re-run on the SAME dump, with the UNMODIFIED script** — the definitive A/B.
   `bash scripts/dr/rehearse_upgrade.sh --report <scratchpad>/rehearsal-b2fix1.md set-20260907T200001Z` → exit 0
   ```
   Running upgrade a4e91c7d2b58 -> b6e5d1c94a72, dispatch_intents.node_url + a deterministic, NOT NULL scrapyd_job_id
   ── step: alembic upgrade head (as crawmatic_migrate) — PASS (5s)
   ── step: provision_db_roles.py --provision (pass 2, post-migrate) — PASS (2s)
   ── step: preflight_regex_profiles.py — PASS (1s)
   verify_grants: checked 3 role(s) ... crawmatic_app/auth/scraper: 58 table(s) in manifest
   ── step: verify_grants.py — PASS (0s)
   === upgrade rehearsal of set-20260907T200001Z PASSED — all 10 steps green ===
   rehearsal container dr-rehearse-4014763 and its data volume removed
   ```
   Same backup set B10 used, same script, no manual SQL. `verify_grants.py`: **0 MISSING** (was 17),
   only the 3 known benign WARNs (partition children + `workspace_usage_v`). `ALTER COLUMN
   scrapyd_job_id SET NOT NULL` passed on the same 2,040 rows that made it fail before — that
   statement is a full-relation DDL re-scan, so it cannot pass unless the rewrite actually reached
   every row. `docker ps -a` clean afterwards; disk unchanged at 1.9 GB free.

4. **RED checks (the tests genuinely catch the bugs)** — the migration was temporarily reverted to
   its `UPDATE` form and one table removed from a grant array; both were restored immediately.
   ```
   FAILED tests/unit/test_migration_offline_dispatch_intent_backfill.py::test_backfill_uses_no_dml_against_the_rls_protected_table
   FAILED tests/unit/test_migration_offline_dispatch_intent_backfill.py::test_type_change_carries_the_backfill_in_its_using_expression
   FAILED tests/unit/test_grants_manifest_schema.py::test_provisioning_sql_covers_every_table_the_manifest_reviews[crawmatic_app]
   ```

5. Rendered migration after the fix (`alembic upgrade a4e91c7d2b58:b6e5d1c94a72 --sql`, offline):
   ```
   ALTER TABLE dispatch_intents ADD COLUMN node_url TEXT DEFAULT '' NOT NULL;
   ALTER TABLE dispatch_intents ALTER COLUMN scrapyd_job_id TYPE UUID USING CASE WHEN scrapyd_job_id ~ '^[0-9a-fA-F]{8}-?...$' THEN scrapyd_job_id::uuid ELSE intent_id END;
   ALTER TABLE dispatch_intents ALTER COLUMN scrapyd_job_id SET NOT NULL;
   ```
   No DML against `dispatch_intents` remains.

## Deviations
1. **Fix technique for Finding 1 is the DDL rewrite, not "run the UPDATE as `crawmatic_auth`" or
   `SET LOCAL row_security = off`** (the two options B10's notes floated). Reasons, all verified:
   `row_security = off` cannot work here — under `FORCE ROW LEVEL SECURITY` even the table owner is
   subject to policy, and Postgres *errors* rather than bypassing when that GUC is off; making the
   migration connect as a second (BYPASSRLS) role means Alembic holding a credential for
   `crawmatic_auth`, which the role model exists to prevent; and `NO FORCE ROW LEVEL SECURITY`
   around the UPDATE would disarm the table's isolation mid-migration. Folding the value change
   into the type rewrite's `USING` needs none of that, is one statement instead of three, is
   atomic with the type change, and is the same technique `a4e91c7d2b58` already used in this repo
   for the identical problem. Deviation impact: NONE (same end state, strictly fewer privileges).
2. **Finding 2 fixed by extending the arrays + a test that makes them derived, not by generating
   the SQL from the YAML.** The repo has no existing generator for this file and
   `provision_db_roles.sql` must stay byte-identical under `psql -f` and `provision_db_roles.py`
   (no meta-commands, no runtime YAML read) — a generator would have been a new build step. The
   unit test instead makes the three files (`rls_table_manifest.txt` → `grants_expected.yaml` →
   `provision_db_roles.sql`) fail the gate the moment they diverge, which is the same guarantee
   with no new machinery and matches how the yaml↔manifest coupling is already enforced in that
   very file. Deviation impact: MINOR (the packet said "prefer deriving if the repo already has
   such a mechanism; otherwise extend them" — it does not, so they were extended, with the
   divergence now impossible to ship unnoticed).
3. **Files touched outside the literal three named in the task**: two test files (the tests the
   task asked for) and nothing else. `scripts/sql/grants_expected.yaml` and
   `scripts/rls_table_manifest.txt` were already correct (B3/B4/B5 updated them) and were NOT
   modified.

Deviation impact: MINOR

## Blockers
none. Docker was run as root (the documented `mahmoud`-not-in-`docker`-group gap, carried in the
release doc's owner items); the rehearsal used its own throwaway container on a random loopback
port, never the shared compose ports, and the script tore it down itself. Disk stayed at 1.9 GB
free, above the script's 1 GiB refusal floor. Nothing deployed, no Railway variable touched, no
production database contacted, $0 spent, nothing committed or pushed.

## Notes for reviewer
- **The unit suite was split into three chunks only because a single run (~630 s) exceeds this
  worker's 600 s foreground cap** and background execution is forbidden by the run's rules. The
  chunks are disjoint and exhaustive over `tests/unit`, and the totals reconcile exactly with
  B10's baseline; a reviewer with more headroom can re-run the single canonical command.
- **A latent, PRE-EXISTING instance of the same class of bug exists in older migrations** and was
  deliberately NOT touched (out of scope, and they are already applied in production): the rendered
  `upgrade head` still contains DML against RLS'd WORKSPACE tables in `5b9a86717a66`
  (`UPDATE api_keys`), `88d894a2e23b` (`INSERT INTO scrape_profile_revisions`) and
  `f30c60cfa2f7`-era revisions (`INSERT INTO domain_strategy_methods`, `UPDATE
  domain_strategy_profiles`, `UPDATE strategy_attempt_stats`). They predate A3's `crawmatic_migrate`
  role model (migrations used to run as a superuser-equivalent), so they were fine when applied —
  but a **fresh-restore / DR replay from an empty database through the whole chain, run as today's
  `crawmatic_migrate`, would silently no-op every one of them**. Worth a follow-up audit task; the
  new offline test in this task is scoped to `b6e5d1c94a72` precisely so it does not fail on these.
- The migration's docstring now carries the full reasoning, including the non-obvious trap that a
  `SELECT`-based self-check inside a migration on a fail-closed table *passes* exactly when the
  backfill did nothing. Worth preserving if the file is edited again.
- `scripts/verify_grants.py` was not modified; it is the acceptance signal and it now reports 0
  MISSING against a real restored production copy.
- The release doc keeps every line of B10's rehearsal evidence; only the two "mandatory step"
  blocks changed meaning (to verification), plus the new green re-rehearsal section and the closed
  owner item.
