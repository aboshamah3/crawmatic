# Autospec Decisions — SPEC-08 Jobs & Orchestration

Log of auto-answered questions and informed defaults. Format:
`- [step] Q: <question> → A: <answer> (source: doc §<section> | default)`

## specify

- [specify] Q: Which run endpoints are in scope for this spec? → A: match- and variant-scoped only (`POST /v1/jobs/run/match/{id}`, `POST /v1/jobs/run/variant/{id}`); product/group/competitor/workspace runs + scheduler are later specs (source: doc §35 "08 — Jobs & Orchestration" acceptance criteria + §36 order — scheduler is spec 13).
- [specify] Q: Does this spec include price analysis / current-price update / alerting? → A: No — ends at persisting job/target outcomes; price_analysis + alert logic are SPEC-09 (source: doc §35 "09 — Current Prices & Alert Logic").
- [specify] Q: How are node selection and duplicate dispatch handled? → A: deterministic node selection (hash by domain or persisted-node round-robin) + idempotent dispatch guard keyed on job/batch; stalled-batch detection re-dispatches after timeout under the same guards (source: doc §26 Celery Queues → scrape_dispatch "Node handling within each Scrapyd pool").
- [specify] Q: How are job counters maintained? → A: aggregated from scrape_job_targets periodically and at finalization, never incremented per-target (source: doc §26 price_analysis note + §35 covers "job counters aggregated from targets").
- [specify] Q: How is Celery fork-safety handled? → A: dispose inherited engine on worker_process_init before first use (source: doc §4/§35 "Celery engine fork-safety"; §2 principle on fork-safety).
- [specify] Q: What HTTP batch size for grouping? → A: 50–200 matches per HTTP batch, grouped by workspace/competitor-domain/scrape-mode; never one Scrapyd job per URL (source: doc §27 Batching Strategy).
- [specify] Q: How is a "batch" represented (table vs derived)? → A: left to planning; binding requirements are deterministic-node + idempotency guarantees, not a specific schema (default — spec §Assumptions).
- [specify] Q: Live infra verification approach? → A: unit tests + integration tests that skip cleanly (no Docker/Postgres/Redis/Scrapyd in build env), consistent with SPEC-01…07 (source: project convention + prior state file deferred_verifications).

## clarify

- [clarify] Q: What type/source for direct API run endpoints? → A: type=MANUAL, source=API (operator on-demand); API_TRIGGERED/PLUGIN/SCHEDULED reserved for programmatic/scheduler triggers (default — doc §22 lists the enum values but not the mapping; informed guess, integrated into spec FR-010).
- [clarify] Q: How does a zero-active-match scoped run resolve? → A: create job, total_targets=0, no dispatch, finalize COMPLETED (default — doc §25 silent on empty case; chosen for observability + idempotency, integrated into US2-AS4 + FR-020).
- [clarify] Q: Stall-timeout value + idempotency-guard storage (Redis vs DB)? → A: deferred to planning (config/implementation); binding reqs are detect+re-dispatch past configured timeout and no-double-run (source: doc §26 states the behavior, not the constant).
- [clarify] No questions relayed to user — all ambiguities resolved doc-first / by informed default with clear reasonable defaults; none high-impact enough to require human decision.
