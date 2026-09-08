# Handover: plan-core-production-readiness-2026-09-07   2026-09-08 01:10   context ~20% (owner-requested, not guard-triggered)   handover #1

## Where the run is
- Phase 0 done, committed d824f31 (review PASS). Stage A done, committed 8960632 (review PASS on pass #2 after one fix: A1 proxied-leg selection).
- Stage B: tasks B1–B9 all DONE-WITH-DEVIATIONS, attempt 1 each, **uncommitted in the working tree** (base 8960632; 104 changed paths outside .epa). B10 (PB-9, release-2 doc + rehearsal) not yet dispatched. Phase B gate not yet run.
- Alembic chain on branch: c8d2e3f4a5b6 → a2f0217c9d43 → 193ac27f0dc2 → b6f1c40a97d2 (A) → a4e91c7d2b58 (B1) → b6e5d1c94a72 (B2) → 096ff6d343b5 (B4) → d3f7a1b62c85 (B3) → e7b34c0af219 (B5). Single head verified by B6.
- Last full unit suite: 4415 passed, 19 skipped (B6, after all B work). Stages C (10 packets) and D (4 packets) not started.

## In flight at handover
none — B6 (last agent) returned and was consumed. Reports B1–B9 exist under reports/.

## Open decisions / escalations
none pending a Hard Stop. Owner items live in BLOCKERS.md (remote tag push; A1 image build after disk relief; A3 grants decision before A10 step 6; B1 replay test reachability) and ASSUMPTIONS.md (A8 rounding, B7 abuse_limit kept on Postgres).

## Do NOT redo
- Phases 0 and A: committed; never re-run.
- B1–B9: implemented and verified, uncommitted. `git status --porcelain` must show them; review, do not re-implement. Do not `git checkout --` anything.
- `git stash@{0}` is unrelated 7a26c54-content junk (droppable, harmless).

## Next action (verbatim from STATE.md)
On resume: (1) verify `git status --porcelain` shows the uncommitted B1–B9 work and `ls reports/` has B1–B9; (2) dispatch B-w6: PB-9 (sonnet, B10 release-2 doc + rehearsal; base 8960632; tell it head is e7b34c0af219 and to carry BLOCKERS/ASSUMPTIONS owner items incl. B1 deferred replay test, B7 abuse_limit, B9 rules gauge wiring, `test_fair_scheduling.py` port 55498 skip); (3) phase B gate: opus reviewer over PB-1..PB-9 with focus list below; PASS → scan, add reviewed files, commit `EPA phase B` → Stage C wave C-w1 (PC-1 opus ‖ PC-9 opus).

## Notes for the next orchestrator
- Phase B reviewer focus: B1 spool durability + the deferred live-DB replay test (try to run it; B2/B3 reached a compose/throwaway Postgres as root); B2 five-step protocol + outbox; B3 fair-mode default flip and per-rule backoff; B5 lease release on both paths; B6 unreachable-pool fallback deviation vs plan; B7 abuse_limit left on Postgres; B9 rules that read gauges via getattr are inert until snapshot wiring (follow-up, not a fail); config.py was appended by B1/B2/B4/B5/B6 — check for duplicates.
- Workers on sonnet stop early if they launch pytest in the background: every dispatch prompt must say "never run_in_background or Monitor; foreground with timeout 600000". If a worker returns without a summary block, SendMessage it once with that instruction.
- `secret-scan.sh` false-positives on the literal path `disk-inventory-2026-09.json` (its `sk-` rule). Classify hits with a sed loop; commit only when every hit is that path.
- The auto-mode classifier blocks `git checkout --`, `git reset --hard`, force pushes and `rm` of tracked files for the orchestrator; plan around it (reviewer file lists + `git add`).
- Docker: `mahmoud` is not in the docker group; docker-backed integration tests run only as root. Disk ~97%; image builds deferred.
- Stage C/D: spend steps (C2.2, C3.2, C4.2, D3.1) and staging/deploy steps are deferred by Pre-Flight; workers ship scripts + commands only.
- Opus packets take 40–90 min wall clock; reports average ~250k subagent tokens.
