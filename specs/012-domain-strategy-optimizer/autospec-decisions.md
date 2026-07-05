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
