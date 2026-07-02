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
