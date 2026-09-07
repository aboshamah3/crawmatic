# Phase 0 Review: Preconditions
Verdict: PASS
Base SHA: 7a26c54   Reviewed: main tree `/srv/crawmatic/crawmatic` (branch `epa/plan-core-production-readiness-2026-09-07`, HEAD `fc9d42a`, uncommitted work in tree)

## Integration command
`sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"` → exit 0
```
tests/unit/test_release_identity.py: 35 warnings
tests/unit/test_version_endpoint.py: 1 warning
tests/unit/test_write_release_manifest.py: 10 warnings
  .../alembic/config.py:612: DeprecationWarning: No path_separator found in configuration...

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
4011 passed, 19 skipped, 6 deselected, 63 warnings in 521.26s (0:08:41)
```
Matches the workers' own reported counts (4011 passed / 19 skipped / 6 deselected) exactly. Baseline before this phase (re-derived from report 0.4): 3992 passed / 19 skipped / 5 deselected — the +19 passed / +1 deselected delta is fully accounted for by the new tests (8 `test_config_diff_railway.py` + 5 `test_write_release_manifest.py` additions + 6 `test_unit_environment_isolation.py`, with its 7th `integration`-marked test explaining the deselection bump).

Also re-ran, independently: `sudo -u mahmoud .venv/bin/pytest tests/unit/test_dr_inventory.py tests/unit/test_config_diff_railway.py -q -p no:cacheprovider` → exit 0 (9 passed). `sudo -u mahmoud git rev-parse main` and `git rev-parse v2026.09.03-readiness^{commit}` → both `7a26c546fb617629badb6e5c600222a3c7081559`, confirming 0.2's report.

## Per-task
| Task | Criteria met | Evidence quality | Notes |
|---|---|---|---|
| 0.1 — Backup inventory (F21) | Yes | Strong — report shows exact pytest exit codes, real read-only run output (125 rows, evidence JSON path), and confirms no `os.remove`/`shutil.rmtree`/`Path.unlink` in the script (independently re-verified: `grep -n "unlink\|rmtree\|os.remove\|prune"` on `scripts/dr/inventory_backups.py` returns nothing). Step 6 owner-gate `rm` list and docker-prune commands are written into the report, nothing executed. | Code read in full: `build_inventory()` matches the interface exactly (`path, bytes, mtime, in_archive, sha256_matches_archive, generation, keep_reason`), archive opened once, `keep_reason` precedence matches the plan's spec. Test is byte-identical to the plan's step-1 snippet plus the repo's standard `sys.path` boilerplate. |
| 0.2 — Fast-forward `main` (OWNER GATE) | Yes | Strong — read-only git inspection, independently re-verified `git rev-parse main` and `git rev-parse v2026.09.03-readiness^{commit}` both resolve to `7a26c546fb617629badb6e5c600222a3c7081559`. Remote tag staleness (still `eb23dff`) correctly identified as an owner item already in BLOCKERS.md, not re-litigated as a task failure. | No files changed; no git mutation performed; exact deferred owner command recorded. |
| 0.3 — Release manifest + config diff (F20 part 1) | Yes | Strong — 8 new tests for `config_diff_railway.py` (secret-stripping, determinism, required-vs-pinned split all covered) + 5 new tests for the manifest extension (buffer/spool/blocklist versions, toolchain, self-hash coverage), all read and independently confirmed to assert what the report claims. Real demo run shown with fabricated names, no Railway credentials. | `config_diff_railway.py` never reads or prints a value — verified by code read: values are truncated at the first `=` before ever being bound to a name; a test asserts a planted secret appears in neither the JSON nor stdout/stderr. |
| 0.4 — Isolated unit-test environment (audit §2) | Yes | Strong — before/after pass counts recorded exactly, exit codes shown for the isolation guard test in both directions (`-m integration` and `-m "not integration"`), and for the two repaired Redis test files. Independently re-ran the full unit gate myself; result matches the report exactly. | `tests/conftest.py` read in full: session-scoped autouse fixture is correct (uses `pytest.MonkeyPatch.context()` since `monkeypatch` can't be requested session-scoped), settings cache cleared on both entry and exit, per-test (not per-session) integration exemption avoids the "any integration test in the run disables isolation for everyone" trap. `docs/ops/TESTING.md` documents both commands and the full marker policy including the not-yet-registered `browser`/`load`/`benchmark` markers. |

## Deviations assessed
All four reports self-labelled MINOR or NONE deviations. Reviewed each against actual impact:
- 0.1: `sys.path` boilerplate in the test (needed — `scripts/` is an unpackaged namespace dir, matches existing convention) and using plain `sudo` instead of `sudo -u mahmoud` for the real run (required — `backups/dr/` is root-owned `0700`, and the plan's own step-4 command literally says `sudo`, not `sudo -u mahmoud`). Both correctly MINOR, not material.
- 0.3: `libs/shared/app_shared/netledger/buffer.py` modified outside the packet's listed file scope. Read the diff — it is a single additive constant (`BUFFER_SCHEMA_VERSION = 1`) plus an `__all__` entry, non-behavioural, required by the acceptance criterion itself ("the netledger buffer schema version constant from `libs/shared/app_shared/netledger/buffer.py`" — no such constant existed). Correctly MINOR.
- 0.3: extra `toolchain` manifest section and `required_and_missing` diff key, beyond the plan's literal two/three-key interfaces. Both are additive projections of specified data (test asserts the subset relation for `required_and_missing`); nothing the plan specified was dropped or renamed. Correctly MINOR.
- 0.4: `tests/conftest.py` created rather than modified (CONTEXT.md's claim that it "exists" was wrong — confirmed no `conftest.py` existed anywhere in the repo pre-phase). Auto-marking `tests/integration/` as `integration` in `pytest_collection_modifyitems`, not literally named in the plan, is load-bearing: without it, CI jobs that run integration files directly with no `-m` filter would get the dead-port environment. Both correctly MINOR — same end state the acceptance criterion requires, no regressions introduced.

No deviation was found to be actually MATERIAL. No unauthorized destructive action found (0.1 never deletes; 0.2 never mutates git state; the only authorized destructive action — the pre-phase fast-forward/push of `main` itself — was executed by the orchestrator per ASSUMPTIONS.md answer 1, not by these workers).

## Secrets check
Read `scripts/config_diff_railway.py` and its test file in full: values are discarded at the first `=` before being bound to a name; a test plants `SOME_TOKEN=super-secret-value` and asserts it appears in neither the JSON output nor stdout/stderr. No secret values appear in any of the four reports, in `scripts/dr/inventory_backups.py`, or in the evidence JSON (`/srv/crawmatic/evidence/disk-inventory-2026-09.json`, outside the repo, not committed — spot-checked: file paths/sizes/timestamps only).

## Cross-task check
No cross-task breakage: 0.3 and 0.4 both touch the unit suite (0.3 adds tests, 0.4 adds the isolation fixture + fixes two ambient-env-dependent test files) and the combined unit gate is green. `pyproject.toml` is untouched (confirmed via `git diff 7a26c54 --stat -- pyproject.toml` — empty), so the plan's later `browser`/`load`/`benchmark` marker registration (A1/B7/C7) is correctly left for those tasks, matching CONTEXT.md's own note.

## Minor observations (non-blocking)
- `scripts/dr/inventory_backups.py` and `tests/unit/test_dr_inventory.py` are owned `root:root` (mode 644, world-readable) while every other new/modified file in this phase is `mahmoud:mahmoud`. Not a functional problem — `sudo -u mahmoud git add` reads them fine (confirmed: `git status --porcelain` as mahmoud lists them normally) — but worth normalizing ownership before it becomes confusing later.

## Files to commit
scripts/dr/inventory_backups.py
tests/unit/test_dr_inventory.py
scripts/config_diff_railway.py
tests/unit/test_config_diff_railway.py
scripts/write_release_manifest.py
libs/shared/app_shared/netledger/buffer.py
tests/unit/test_write_release_manifest.py
tests/conftest.py
docs/ops/TESTING.md
tests/unit/test_unit_environment_isolation.py
tests/unit/test_rate_limiter.py
tests/unit/test_stats_buffer.py

## Follow-ups (non-blocking)
- Owner gate: 0.1 step 6 disk deletion (per-row `rm` list + `docker image prune`/`docker builder prune`) — deferred, evidence at `/srv/crawmatic/evidence/disk-inventory-2026-09.json`.
- Owner gate: 0.2 remote tag force-push (`git push --force origin refs/tags/v2026.09.03-readiness`) — deferred, already in BLOCKERS.md.
- Owner gate: 0.3 real Railway config diff (`railway variables --service <s> --kv | cut -d= -f1` per service, then `config_diff_railway.py`) — deferred, no Railway credentials used.
- `scripts/dr/inventory_backups.py` / `tests/unit/test_dr_inventory.py` ownership normalization (`chown mahmoud:mahmoud`) before the next phase touches them, for consistency only.
- `config_diff_railway.py` lets a `PermissionError` on an unreadable names file propagate as a traceback instead of exiting 2 (only non-existence is handled) — cosmetic, flagged by the 0.3 worker itself, no fix needed now.
- `browser`/`load`/`benchmark` pytest markers remain unregistered in `pyproject.toml` by design (owned by A1/B7/C7) — tracked in `docs/ops/TESTING.md` so nothing is lost.
