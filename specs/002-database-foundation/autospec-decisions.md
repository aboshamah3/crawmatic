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
