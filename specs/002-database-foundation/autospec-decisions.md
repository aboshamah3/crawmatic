# Autospec Decisions — SPEC-02 Database Foundation

Feature directory: `specs/002-database-foundation`
Master doc: `/srv/crawmatic/PROJECT_SPEC.md`

## specify

- [specify] Q: Any clarifications needed? → A: No NEEDS CLARIFICATION markers; every requirement fully specified by the doc (source: doc §19 Money, §21 ID Strategy, §22 Database Models conventions, §32 Workspace Isolation, §35 subsection "02 — Database Foundation").
- [specify] Q: Feature short-name / directory? → A: `specs/002-database-foundation` (sequential numbering, second spec) (source: default + doc §5 dir name `02-database-foundation`).
- [specify] Q: Scope — does SPEC-02 include real domain tables? → A: No. Foundation/patterns only + a demonstration/smoke model+migration to prove the machinery; real tables (workspaces/users/products) are SPEC-03+ (source: doc §22 lists tables but §35 "02" scopes this to foundation; §35 "03" onwards adds tables).
- [specify] Q: Money scale? → A: NUMERIC(18,4), finite only, reject over-scale (source: doc §19).
- [specify] Q: Enum representation? → A: string-backed, application-validated (no DB-native enums) (source: doc §22).
- [specify] Q: Migration connection routing? → A: one-shot job direct-to-Postgres (not via PgBouncer); apps never migrate at startup; single linear history, CI fails on multiple heads (source: doc §22, §4, §6).
- [specify] Q: Live-Postgres acceptance items given no Docker daemon here? → A: author + unit/static validate here; run migration-job + connectivity check on a Postgres-capable host. DB-independent behavior (UUIDv7, money validation, naming-convention, no-eager-engine) fully verifiable here (source: project memory no-docker-daemon-in-build-env).

## clarify

No questions relayed to the user — doc + SPEC-01 skeleton resolved every material ambiguity. Doc-resolved clarifications recorded in spec.md `## Clarifications` (Session 2026-07-02):

- [clarify] Q: Sync vs async SQLAlchemy + driver? → A: sync SQLAlchemy 2.0 over psycopg 3 (formalizes SPEC-01 pattern) (source: §3, SPEC-01).
- [clarify] Q: UUIDv7 production? → A: app-generated UUIDv7 PK default; lib is plan-level (source: §21).
- [clarify] Q: Money? → A: NUMERIC(18,4), Decimal, finite-only, reject over-scale, never float (source: §19).
- [clarify] Q: Enums? → A: string-backed, app-validated, no DB-native enums (source: §22).
- [clarify] Q: Migration routing/history? → A: one-shot job direct-to-Postgres; apps never migrate at boot; single linear history, CI multi-head guard (source: §22/§4/§6).
- [clarify] Q: RLS at foundation stage? → A: RLS-ready workspace base + RLS-policy-DDL helper (fail-closed, SET LOCAL); first real policy in SPEC-03 (source: §32).
- [clarify] Q: Prove machinery w/o domain tables? → A: demo/smoke model+migration (UUIDv7/TIMESTAMPTZ/two-uniques naming); exact form, Alembic config, UUIDv7 lib, "core enums" set = plan-level (source: §22/§35).
- [clarify] Q: Live-Postgres items here? → A: author + unit/static validate; run migration/connectivity on Postgres host (source: no-docker-daemon constraint).

## plan (opus subagent)

- [plan] UUIDv7 → `uuid6>=2025.0.1,<2026` (`uuid6.uuid7()`), wrapped `new_uuid7()`, stored as Postgres UUID. Verified: UUID subclass, version==7, time-ordered (source: default — §21 left lib open).
- [plan] naming_convention (all 5 keys, `column_0_N_name` all-columns token): verified live that uq(workspace_id,external_id) vs uq(workspace_id,sku) → distinct names `uq_products_workspace_id_external_id` / `uq_products_workspace_id_sku`. 63-char truncation caveat noted (source: §22).
- [plan] Money → `Money(TypeDecorator)` over Numeric(18,4); rejects float/NaN/Infinity/over-scale; returns Decimal (source: §19).
- [plan] Naive-timestamp guard → `TZDateTime` TypeDecorator raising on tzinfo None + DateTime(timezone=True) (source: §22).
- [plan] RLS → WorkspaceScopedBase mixin + emit_rls_policy() (ENABLE+FORCE+fail-closed USING current_setting('app.workspace_id',true)::uuid); no real table here (source: §32).
- [plan] Demo/smoke → tiny permanent `_smoke_foundation` table + first Alembic migration; DB-independent proof via metadata asserts + offline `alembic upgrade head --sql`; live parts deferred to Postgres host (source: §22/§35 + no-docker constraint).
- [plan] Migration job → new optional MIGRATION_DATABASE_URL (direct postgres:5432) consumed by alembic/env.py; one-shot apps/migrate image + compose `migrate` service (restart:no); scripts/check_single_head.sh CI guard (source: §4/§6/§22).
- [plan] Constitution Check → PASS (II/VII/VIII satisfied; app_shared import boundary kept scrapy-free). Artifacts: plan.md, research.md, data-model.md, quickstart.md, contracts/{models-base,ids,money,enums,rls,migration-job,config}.md.

## checklist

- [checklist] Q: focus/depth/audience? → A: DB-foundation correctness & integrity (9 focus areas); Standard depth; Reviewer pre-implementation gate. No user clarifying questions (args fully specified).
- Generated checklists/data-foundation.md (30 requirements-quality items).
- Gap found + fixed before checking: migration-failure atomicity/rollback was unspecified → added spec FR-016 + Edge Case (each migration in a transaction, rolls back to prior revision; downgrade path; non-transactional migrations must document non-atomicity — none here).
- Completion: data-foundation.md 30/30 pass; requirements.md 16/16 pass. Implement gate CLEAR.

## analyze (inline, forked)

0 CRITICAL/HIGH → no user pause. Remediated all actionable findings myself:

- [analyze] I2 (MEDIUM, NON-NEGOTIABLE Principle II): RLS predicate `''::uuid` would RAISE on an empty `app.workspace_id` instead of failing closed → A: changed emitter to `NULLIF(current_setting('app.workspace_id', true), '')::uuid` so absent AND empty both → NULL → zero rows. Fixed contracts/rls.md, tasks T008/T014, and aligned research.md/plan.md/data-model.md/quickstart.md (removed all bare-predicate drift).
- [analyze] I1 (MEDIUM): FR-004 "naive columns forbidden at base level" only enforced at value level (TZDateTime) → A: added a structural mapper-config guard (event listener raising when a mapped column is a timezone-naive DateTime) in T007 + a test in T013 that a naive-column model raises at class/mapper config.
- [analyze] G1 (LOW): fork-disposal (FR-009) untested → A: T020 now asserts dispose_engine() resets singletons + worker_process_init hook wired (mocked engine, no DB).
- [analyze] G2 (LOW): pooler-safe config (FR-010) unasserted → A: T020 asserts connect_args prepare_threshold is None.
- [analyze] A1 (LOW): check_connection signature undecided → A: pinned `-> None` (raises on failure) in T018.
- [analyze] A2 (LOW): enum mechanism ambiguous → A: pinned plain String column + application-side validation (per §22), not Enum(native_enum=False); T009/T015 updated.
- [analyze] N1 (LOW): env-var name drift → A: standardized on MIGRATION_DATABASE_URL in plan.md (dropped TEST_DATABASE_URL).
- [analyze] N2 (LOW): plan tree missing models/_smoke.py → A: added.
- [analyze] G3 (LOW): FR-016 downgrade untested → A: added downgrade round-trip to deferred live T033.
- Only MEDIUM/LOW fixed (no CRITICAL/HIGH) → full analyze re-run not required by pipeline; self-verified NULLIF consistency across all 6 artifacts (0 bare predicates remain). 100% FR/SC coverage retained.

## implement (sonnet subagents, grouped phases)

34/38 tasks [X]; 4 DEFERRED (live Postgres). Suite: 47 passed, 7 skipped.
- Phases 1-2 (T001-T005): uuid6+alembic deps, uv.lock, MIGRATION_DATABASE_URL config + .env.example.
- Phase 3 US2 (T006-T015): ids/base/rls/enums/models + 5 unit tests. Verified live: two shared-first-column uniques → DISTINCT names; structural naive-column guard raises at mapper-config; RLS NULLIF fail-closed.
- Phases 4-5 (T016-T020; T021 deferred): Money TypeDecorator (reject float/NaN/Inf/over-scale), check_connection, engine-hygiene tests (no-eager-engine, fork-disposal, pooler-safe, Celery hook).
- Phase 6 US1 (T022-T032; T033/T034 deferred): _smoke_foundation model + first migration (offline render: 2 distinct uq names, TIMESTAMPTZ, NUMERIC(18,4)), alembic env.py (direct URL), single-head guard, apps/migrate one-shot image + compose migrate service.
- Phase 7 Polish (T035, T037; T036 deferred): final validation, scope sweep (Base.metadata == ['_smoke_foundation'] only).

### DEFERRED — blocked on live Postgres / Docker daemon (unavailable here)
- T021 connectivity SELECT 1; T033 online upgrade + downgrade round-trip; T034 compose one-shot migrate run; T036 RLS fail-closed live behavior. Artifacts authored + offline/statically validated. Run on a Postgres/Docker host to close.

## converge (opus subagent + inline fix)

- Result: NEW TASK APPENDED — T038: gate the one-shot `migrate` compose service behind `profiles: ["migrate"]` (cross-spec fix: without it `docker compose up` starts a 9th service, contradicting FR-011's explicit-one-shot intent and breaking SPEC-01's exactly-8-services smoke test).
- Applied T038 inline (trivial one-key YAML edit) + verified: default `docker compose config --services` = 8 (SPEC-01 invariant preserved); `--profile migrate` = 9; test_no_startup_migrations + unit suite pass (46). Marked T038 [X].
- Static sweep (converge): one alembic head; offline DDL render OK; check_single_head exit 0; Base.metadata == ['_smoke_foundation']; NULLIF fail-closed rendered; app_shared scrapy-free. FR-001..FR-016 + SC-002..SC-006 built/verified here; SC-001 + live SC-007/FR-015/FR-016-downgrade = the 4 daemon-deferred tasks.
- Converged after applying the single appended task (cycle 1); no implement re-loop needed.
