# Packet PC-4 — Phase C: Stage C — Efficient and correct results/cost
Model: opus   Parallel-safe: no   Depends on: PC-3
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: C-w3   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### C5 — Structured offer contract on the live path; ranker shadow; durable evidence (F19)
Acceptance criteria:
- `ScrapeResult` gains `offer: OfferObservation | None`, `extractor_version: str`, `profile_version: int`, `confidence: Decimal`, `provenance: str`.
- `EXTRACTION_RANKING_POLICY: Literal['off','shadow','v1'] = 'shadow'`; in `shadow`, `extract` returns the first-hit AND runs `extract_ranked`, writing disagreements to `extraction_shadow_events`. The first-hit price is what gets persisted.
- `pipelines.py` (~`:398-460`) populates the `offer_*` columns and `offer_raw_evidence_hash` from `store_evidence(html_bytes, store_dir=EVIDENCE_STORE_DIR)`; a persisted observation's hash resolves via `resolve_hash`.
- `pipelines.py:191` `_monotonic_conflict_where` additionally refuses to replace a current price whose `confidence` is higher than the incoming one unless the incoming is newer by > 24 h.
- The evidence retention sweep `MAINTENANCE_EVIDENCE_RETENTION` deletes blobs older than `EVIDENCE_RETENTION_DAYS = 30` ONLY when no observation younger than its own retention still references the hash (keeps a blob referenced by a 10-day-old observation; deletes an unreferenced 40-day-old one).
- Two Alembic revisions: `extraction_shadow_events`, and observation offer/provenance columns (`extractor_version`, `profile_version`, `confidence`, `provenance`, `availability_state TEXT` with values `available|unavailable|blocked|stale|conditional`). Chain them one after the other; a single head at the end.
- `scripts/run_offer_benchmark.py` gains `--from-shadow-events`. Switching `EXTRACTION_RANKING_POLICY` to `v1` is an OWNER decision at C11 - the default stays `shadow` here.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task C5: Structured offer contract on the live path; ranker shadow; durable evidence (F19)
>
> **Files:**
> - Modify: `libs/scrape-core/scrape_core/items.py` (`ScrapeResult.offer: OfferObservation | None`, `extractor_version: str`, `profile_version: int`, `confidence: Decimal`, `provenance: str`)
> - Modify: `libs/scrape-core/scrape_core/extraction/pipeline.py:123-160` (callers pass `ranking_policy` from `EXTRACTION_RANKING_POLICY: Literal["off","shadow","v1"] = "shadow"`; in `shadow`, `extract` returns the first-hit and **also** runs `extract_ranked`, writing disagreements to `extraction_shadow_events`)
> - Modify: `libs/scrape-core/scrape_core/pipelines.py:398-460` (populate `offer_*` columns and `offer_raw_evidence_hash` from `store_evidence(html_bytes, store_dir=EVIDENCE_STORE_DIR)`)
> - Modify: `libs/shared/app_shared/observations/evidence_store.py` (`EVIDENCE_STORE_DIR` from settings — a Railway volume mounted on both scraper services; retention sweep `MAINTENANCE_EVIDENCE_RETENTION` deletes blobs older than `EVIDENCE_RETENTION_DAYS = 30` **only** when no observation younger than its own retention still references the hash — closes the gap `RETENTION_POLICY.md` §2.1 records)
> - Create: `alembic/versions/<rev>_extraction_shadow_events.py`, `<rev>_observation_offer_provenance.py` (`extractor_version`, `profile_version`, `confidence`, `provenance`, `availability_state TEXT` with values `available|unavailable|blocked|stale|conditional`)
> - Modify: `libs/scrape-core/scrape_core/pipelines.py:191` (`_monotonic_conflict_where` additionally refuses to replace a current price whose `confidence` is higher than the incoming one unless the incoming is newer by > 24 h)
> - Test: `tests/unit/test_offer_propagation.py`, `tests/unit/test_ranker_shadow_events.py`, `tests/unit/test_evidence_retention_gate.py`, `tests/unit/test_current_price_confidence_guard.py`
>
> - [ ] **Step 1: Failing tests**: a persisted observation has a non-null `offer_raw_evidence_hash` and the blob resolves via `resolve_hash`; in `shadow` mode a page where the ranker's winner differs from first-hit produces one `extraction_shadow_events` row and the first-hit price is what gets persisted; a low-confidence incoming price does not overwrite a high-confidence current price; the evidence sweep keeps a blob referenced by a 10-day-old observation and deletes an unreferenced 40-day-old one.
> - [ ] **Step 2: Implement.** Switching `EXTRACTION_RANKING_POLICY` to `v1` is an owner decision taken in C11 only if the shadow disagreement rate is < 1% on the C4 labeled sets **and** the ranker wins ≥ 99% of labeled conflicts (`scripts/run_offer_benchmark.py` extended with `--from-shadow-events`).
> - [ ] **Step 3:** commit `feat(extraction): structured offer + provenance on the live path; ranker shadow; durable evidence with gated retention (F19)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/C5.md

## Scope
Files/areas in scope:
- **C5** — Modify `libs/scrape-core/scrape_core/items.py`, `libs/scrape-core/scrape_core/extraction/pipeline.py` (~`:123-160`), `libs/scrape-core/scrape_core/pipelines.py` (~`:398-460` and `:191`), `libs/shared/app_shared/observations/evidence_store.py`, `libs/shared/app_shared/config.py`, `scripts/run_offer_benchmark.py`. Create `alembic/versions/<rev>_extraction_shadow_events.py`, `alembic/versions/<rev>_observation_offer_provenance.py`, `tests/unit/test_offer_propagation.py`, `tests/unit/test_ranker_shadow_events.py`, `tests/unit/test_evidence_retention_gate.py`, `tests/unit/test_current_price_confidence_guard.py`.

Conventions:
- Repo root `/srv/crawmatic/crawmatic`, branch `epa/plan-core-production-readiness-2026-09-07`. Run every engine command as `mahmoud`: `sudo -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/pytest ...`.
- The interpreter in `.venv` is **Python 3.13.14** even though the plan text says 3.12. Do not "fix" that.
- Definition of done per the plan: failing test first, implementation, tests green.
- **Do NOT run `git commit`.** The plan's "Commit `...`" steps are for the orchestrator, which commits per phase after review. Leave your changes uncommitted in the main tree (worker contract rule 8) and put the intended commit message in your report. Never `git add -A`.
- **Core only.** Nothing under `/srv/crawmatic/saas` may be modified.
- One Alembic head at all times; every new revision chains from the current head. Partitioned tables never get a bulk `DELETE`; drops are whole partitions.
- Money is micro-USD (1 USD = 1,000,000). The proxy billing unit is a named setting, never an implicit constant.
- Every read/write is workspace-scoped unless it is a registered system sweep (`_system_session()`); new fleet tables without a `workspace_id` are never exposed to tenant roles.
- Secrets: variable NAMES and file locations only, never values. Never run `env`/`printenv`/`set`.

Context (from CONTEXT.md ## Areas):
- **C5** — `items.py` is 169 lines, `extraction/pipeline.py` 412, `pipelines.py` 1035 with `_monotonic_conflict_where` confirmed at `:188-193`, `evidence_store.py` 184. A5 (Stage A) and B1 (Stage B) already edited `pipelines.py` and `items.py` - build on their `attempt_uuid`, timestamps and `ON CONFLICT DO NOTHING` inserts, do not revert them. `EVIDENCE_STORE_DIR` is a Railway volume mounted on both scraper services.

Task-specific notes:
- **C5** — The evidence retention gate closes the gap `docs/RETENTION_POLICY.md` §2.1 records - reference that section in your report.
- **C5** — Do NOT set `EXTRACTION_RANKING_POLICY='v1'`. Shadow mode must not change what gets persisted.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. **Single Alembic head** (this packet adds a revision): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

