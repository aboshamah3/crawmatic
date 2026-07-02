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
