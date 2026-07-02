# Autospec Decisions — SPEC-01 Monorepo & Services Skeleton

Feature directory: `specs/001-monorepo-skeleton`
Master doc: `/srv/crawmatic/PROJECT_SPEC.md`

## specify

- [specify] Q: Are any clarifications needed for the skeleton scope? → A: No NEEDS CLARIFICATION markers; every requirement is fully specified by the doc (source: doc §4 Deployment Services, §5 Monorepo Structure, §6 Service Communication, §35 subsection "01 — Monorepo & Services Skeleton").
- [specify] Q: Feature short-name / directory? → A: `specs/001-monorepo-skeleton` (sequential numbering, first spec) (source: default + doc §5 which names the dir `01-monorepo-services-skeleton`).
- [specify] Q: Redis single vs split instance at skeleton stage? → A: single local instance acceptable now; split broker/locks with noeviction deferred to deployment/later specs (source: doc §4 redis — "at minimum two configs" is a deployment concern; skeleton only needs Redis running).
- [specify] Q: PgBouncer auth mode locally? → A: `trust` allowed locally, `scram-sha-256` required in deployed envs (source: doc §4 pgbouncer "trust is acceptable only on a developer's local machine").
- [specify] Q: One-shot migration direct-to-Postgres exception? → A: Out of scope for SPEC-01 (DB foundation is SPEC-02); noted as future exception to the "all through PgBouncer" rule (source: doc §4/§6 + §35 "02 — Database Foundation").

## clarify

No questions relayed to the user — the master doc resolved every material ambiguity. Doc-resolved clarifications recorded in spec.md `## Clarifications` (Session 2026-07-02):

- [clarify] Q: Packaging/build model? → A: uv workspace, one root pyproject + one lockfile, per-member dependency closure (source: doc §3 Packaging, §5).
- [clarify] Q: Local orchestration mechanism? → A: docker-compose at repo root, same images in deployed envs (source: doc §5).
- [clarify] Q: API health endpoint + port? → A: GET /health, port from env (example 8000) (source: doc §4, §35).
- [clarify] Q: Postgres connectivity at skeleton? → A: all services via PgBouncer transaction pooling (6432); migration direct-connect out of scope (source: doc §4, §6).
- [clarify] Q: Redis instances at skeleton? → A: single local instance now; split broker/locks noeviction deferred (source: doc §4 redis).
- [clarify] Q: Exact Python version + pinned image tags? → A: Deferred to plan (implementation detail; plan picks current stable Python + concrete pinned images) (source: doc §3/§4 — not enumerated in doc).

## plan (opus subagent)

- [plan] Python version → A: 3.13 (`requires-python = ">=3.13,<3.14"`), supported across FastAPI/Celery 5.5/Scrapy 2.13/Twisted 24.x/scrapy-playwright/SQLAlchemy 2.0/psycopg 3.2 as of 2026-07 (source: default — doc left version unspecified).
- [plan] Pinned images → A: python:3.13.5-slim-bookworm, postgres:17.5-bookworm, edoburu/pgbouncer:v1.23.1-p3, redis:7.4.2-bookworm, ghcr.io/astral-sh/uv:0.7.13; Playwright Chromium baked at build. `@sha256` digest pinning flagged as implement-phase follow-up (source: default — doc required "pinned, no latest" but not tags).
- [plan] Constitution Check → PASS (I, V, VI, VIII PASS; II, III, IV, VII N/A-deferred to later specs — no data/business logic in skeleton).
- Artifacts: plan.md, research.md, data-model.md, contracts/{health,service-topology,environment}.md, quickstart.md.

## checklist

- [checklist] Q: Checklist focus/depth/audience? → A: Infra/deployment operational readiness; Standard depth; Reviewer (pre-implementation gate). No user clarifying questions needed (args fully specified focus) (source: doc-derived + provided args).
- Generated checklists/deployment.md (26 items, requirements-quality "unit tests for English").
- Completion: deployment.md 26/26 pass; requirements.md 16/16 pass. No artifact required amendment — implement gate CLEAR (no unchecked checklist items).

## tasks (opus subagent)

- 45 tasks (T001–T045) across 6 phases: Setup (T001–T011), Foundational shared libs (T012–T016), US1 stack bring-up/MVP (T017–T035), US2 boundaries (T036–T039), US3 env config (T040–T042), Polish (T043–T045).
- Explicit Scope Boundary section forbids DB models/migrations/auth/scraping-logic tasks. SC-001..SC-006 mapped to specific tasks in a coverage table. No hooks; no human decisions.

## analyze (inline, forked)

No CRITICAL findings → no user pause. Remediated all actionable findings myself (analyze is read-only):

- [analyze] I1 (HIGH): API_PORT vs PORT could diverge at boot → A: made `API_PORT` the single canonical var; compose injects `PORT=${API_PORT}` and publishes `"${API_PORT}:${API_PORT}"`. Fixed tasks T013/T029/T034 + contracts/environment.md.
- [analyze] U1 (MEDIUM): pgbouncer dual-stack mechanism unspecified → A: added `PGBOUNCER_LISTEN_ADDR=*` (maps to image LISTEN_ADDR) to environment.md + tasks T037/T040.
- [analyze] C1 (MEDIUM): constitution Principle I named `libs/shared` for shared Scrapy code (doc §5 says `libs/scrape-core`) → A: PATCHed constitution to 1.0.1 naming scrape-core + one-way dep rule (aligns with PROJECT_SPEC §5; master doc unchanged).
- [analyze] A1 (LOW): browser concurrency was an unquantified adjective → A: pinned `max_proc=1`, `CONCURRENT_REQUESTS=2`, `PLAYWRIGHT_MAX_CONTEXTS=1` in tasks T027/T028.
- [analyze] G2 (LOW): FR-011 listed Scrapyd nodes though they have no DB access yet → A: added scope note to FR-011.
- [analyze] G1 (LOW): "Postgres-not-ready" edge case has no task → A: no action (moot in skeleton — /health is dependency-free, scheduler/worker carry no DB access); deferred to SPEC-02 when real DB access lands.
- Re-ran analyze (HIGH was fixed): all 5 confirmed resolved. Two new LOW cosmetic issues fixed — N1 (plan.md constitution stamp v1.0.0→v1.0.1), N2 (environment.md FR-013→FR-017 citation). Final: 0 CRITICAL/HIGH, 100% FR/SC coverage.
