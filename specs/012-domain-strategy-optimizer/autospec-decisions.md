# Autospec Decisions — SPEC-12 Domain Strategy Optimizer

Log of auto-answered questions and remediations during the autospec pipeline.
Format: `- [step] Q: <question> → A: <answer> (source: doc §<section> | default)`

## specify

- [specify] Q: Which sections define the authoritative requirements (roadmap §35.12 is terse)?
  → A: Combined §35.12 (roadmap) with §14 (Domain Strategy Optimizer detail), §15 (URL Pattern
  Derivation), and §22 (data models) as the full requirement text. (source: doc §14/§15/§22/§35.12)
- [specify] Q: Any [NEEDS CLARIFICATION] markers needed? → A: None; doc fully specifies behavior.
  Minor unspecified details (configurability of thresholds, discovery trigger paths, counter
  store = Redis, primary store = Postgres) resolved via documented defaults in Assumptions.
  (source: doc §3/§8/§14/§26/§28 + constitution)
- [specify] Q: Numbering/dir? → A: sequential → specs/012-domain-strategy-optimizer.
  (source: .specify/init-options.json feature_numbering=sequential)
- [specify] Q: after_specify hook? → A: Skipped — agent-context hook is disabled in
  extensions.yml (project does not use Copilot). (source: .specify/extensions.yml)

## clarify

Ambiguity scan: master doc §14/§15/§22 resolves nearly all taxonomy categories (Clear). Three
Partial items resolved doc-first — no user questions asked.

- [clarify] Q: Source of the per-attempt learning signal (double-fetch vs existing pipeline)?
  → A: Existing SPEC-07 spider per-attempt path reports into the buffered stats; discovery is
  the only active multi-method probe, on a 3–10 URL sample. (source: doc §14 + SPEC-07)
- [clarify] Q: Window for rediscovery "success rate < 80%" and "3 consecutive failures"?
  → A: recent_failure_count (reset on qualifying success) for consecutive; cumulative
  strategy_attempt_stats.success_rate + pending buffered deltas for success rate. (source: doc
  §22 fields + §14 DB-plus-pending-deltas)
- [clarify] Q: Discovery trigger — auto, operator, or both? → A: Both; converge on one
  discovery-run + profile-seed path; auto path enqueues on strategy_discovery queue. (source:
  doc §14 + §26 strategy_discovery queue)

## checklist

Setup questions auto-answered doc-first: depth = formal pre-implementation gate; audience =
reviewer (autospec orchestrator); focus = learning/promotion correctness, isolation/RLS,
reactor-safety & scale, URL-pattern versioning, rediscovery coverage. Generated
`checklists/optimizer.md` (35 items). Both checklists completed (requirements.md 16/16;
optimizer.md 35/35). Two items surfaced real measurability gaps → spec fixed:

- [checklist] CHK005 → Fixed FR-010: "3 different URLs" now defined as 3 distinct source URLs
  (by distinct full normalized URL string sharing the derived pattern). (source: default —
  doc silent on URL identity)
- [checklist] CHK030 → Fixed FR-020: quantified "repeatedly" (empty-selector / sub-0.75 /
  403-429) as a configurable consecutive-occurrence threshold, default 3, reset on qualifying
  success. (source: default — doc §14 leaves "repeatedly" unquantified)
- [checklist] after_checklist hook? → None registered in extensions.yml; skipped.

## analyze

First pass: 0 CRITICAL. 1 HIGH (F1), 2 MEDIUM (F2, F3), 3 LOW (F4, F5, F6-informational).
Remediated all actionable findings myself (analyze is read-only), then re-ran analyze once.

- [analyze] F1 (HIGH) rediscovery conditions 3/5/6/7/8 had no recorded signal source → Added
  FR-020a (two-source model: aggregate counters via combined_stats; per-attempt outcome via
  `recent_signals` built off-hot-path from existing `request_attempts` — hot-path buffer NOT
  widened) + FR-020b (concrete detection: unrealistic=§18 bound failure; template-change=
  re-derived url_pattern≠profile). Updated contracts/rediscovery.md (evaluator signature +
  RecentSignals), tasks T029/T030/T031/T034/T035. (source: doc §14 + SPEC-07 request_attempts)
- [analyze] F2 (MEDIUM) discovery probed PLAYWRIGHT_PROXY it can't execute → discovery
  skips/short-circuits PLAYWRIGHT_PROXY until SPEC-14. Updated contracts/discovery.md, T027,
  plan.md. (source: plan out-of-scope note + SPEC-14 boundary)
- [analyze] F3 (MEDIUM) "unrealistic price"/"template changed" undefined → defined in FR-020b +
  rediscovery contract. (source: doc §18 validation + §15 pattern re-derive)
- [analyze] F4 (LOW) bare PLAYWRIGHT token drift → removed from plan.md. F5 (LOW) tie-break
  cost order undefined → defined access cost order (ladder order) + §16 extraction order in
  contracts/discovery.md + T027. F6 (LOW) US5-last sequencing → informational, no action
  (feature ships as one release unit).
- Re-run: 0 CRITICAL/HIGH/MEDIUM; 3 LOW (I1 plan cosmetic, A1 FR-010 currency-required pointer,
  A2 SC-003 wording) — all three applied. Cleared for implement.

## implement

8 phases, one sonnet subagent per phase (P8 finished inline by orchestrator after a session-limit
cut-off). All 45 tasks [X]. Full suite green: 1490 passed / 221 skipped / 0 failed; single head
f30c60cfa2f7; scoping guard OK; ruff clean; reactor-safety + import-boundary tests green.

- [implement] P2/T008 regression: `test_repository_scoping.py::test_workspace_owned_models_is_exactly_*`
  asserted exact membership → updated to include DomainStrategyProfile/StrategyDiscoveryRun (correct;
  scoping guard requires them registered).
- [implement] P6/P7 import-boundary: `rediscovery.py` docstrings contained the literal `scrape_core`
  substring, tripping `test_import_boundaries.py`'s static ban → reworded to "scrape-core" (hyphen).
- [implement] P8 design (inline): STRATEGY_STATS_FLUSH was never routed to the maintenance queue in
  celery_app.py (Phase-7 gap) → routed it alongside the new STRATEGY_PATTERN_BACKFILL. Backfill
  re-derives a stale profile's pattern from a representative competitor match URL (matched by
  competitor_id+url_pattern, since matches carry no domain col) → re-link if unchanged else reset to
  DISCOVERY_REQUIRED + enqueue discovery. Not exercised at algorithm version 1 (scan empty).
- [implement] T045 quickstart: Scenarios 1–6 are infra-gated (live Postgres/Redis/Scrapyd) and covered
  by skip-clean integration tests; unit-level SC-001..SC-007 logic (promotion, resolution, rediscovery
  8-condition, buffered-stats no-hot-row, RLS DDL) is exhaustively unit-tested and green.
- No `after_*` hooks fired at any implement step (none registered/enabled).
