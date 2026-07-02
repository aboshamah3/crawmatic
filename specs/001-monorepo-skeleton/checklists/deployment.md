# Deployment & Infrastructure Requirements Quality Checklist: Monorepo & Services Skeleton

**Purpose**: Validate that the SPEC-01 requirements for operational bring-up, network/security boundaries, PgBouncer routing, dependency boundaries, environment config, and scope discipline are complete, clear, consistent, and measurable — before implementation.
**Created**: 2026-07-02
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md) · [contracts/](../contracts/)
**Depth**: Standard · **Audience**: Reviewer (pre-implementation gate)

## Requirement Completeness

- [x] CHK001 - Are boot-to-running requirements defined for all eight components (API, scheduler, worker, both Scrapyd nodes, Postgres, PgBouncer, Redis)? [Completeness, Spec §FR-005–FR-010, §SC-001]
- [x] CHK002 - Is the `GET /health` response contract (shape, status, dependency-free) fully specified? [Completeness, contracts/health.md]
- [x] CHK003 - Is a documented, runnable start command specified for every deployable service? [Completeness, Spec §FR-019, contracts/service-topology.md]
- [x] CHK004 - Is the complete environment-variable catalogue enumerated, identifying which service consumes each variable? [Completeness, Spec §FR-017, contracts/environment.md]
- [x] CHK005 - Are the uv-workspace dependency-boundary rules (`app_shared` excludes Scrapy/Twisted/Playwright; `scrape_core`→`app_shared` one-way) stated as requirements? [Completeness, Spec §FR-003–FR-004]
- [x] CHK006 - Are non-root, pinned-image, and dual-stack-bind hardening requirements specified for each applicable component? [Completeness, Spec §FR-014–FR-016, contracts/service-topology.md]
- [x] CHK007 - Is the uv-workspace packaging model (one root project, one lockfile, per-member dependency closure) captured as a requirement? [Completeness, Spec §FR-002, Clarifications]

## Requirement Clarity

- [x] CHK008 - Is "publicly exposed" made precise (only `api` publishes a host port; all others internal-only via `expose:`)? [Clarity, Spec §FR-013, contracts/service-topology.md §Exposure]
- [x] CHK009 - Are Scrapyd URL pool semantics unambiguous (comma-separated list, treated as a pool even with a single entry)? [Clarity, Spec §FR-018]
- [x] CHK010 - Is engine/process hygiene quantified (one lazy engine per process; never at import time or per request; Celery disposes inherited engine on fork)? [Clarity, Spec §FR-020, plan.md Technical Context]
- [x] CHK011 - Is the local-vs-deployed PgBouncer auth distinction (`trust` local only, `scram-sha-256` in deployed environments) stated without ambiguity? [Clarity, Spec Assumptions, contracts/environment.md]
- [x] CHK012 - Is "no business logic beyond health checks" defined concretely enough to be enforceable (scheduler/worker/Scrapyd boot-only)? [Clarity, Spec §FR-005–FR-009]

## Requirement Consistency

- [x] CHK013 - Do spec, plan, and the topology contract agree on the exact eight-component list and their ports? [Consistency, Spec §FR-001, contracts/service-topology.md]
- [x] CHK014 - Are Scrapyd basic-auth requirements consistent across spec, topology contract, and environment contract? [Consistency, Spec §FR-012, contracts/service-topology.md, contracts/environment.md]
- [x] CHK015 - Does `DATABASE_URL` consistently target `pgbouncer:6432` (never `postgres:5432`) everywhere it appears? [Consistency, Spec §FR-011, contracts/environment.md]
- [x] CHK016 - Is the Scrapy shared-code rule (extraction/pipelines/rate-limiter live in `scrape_core`, imported by both projects) consistent with the dependency-boundary rules? [Consistency, Spec §FR-004, plan.md Structure Decision]

## Acceptance Criteria Quality

- [x] CHK017 - Are success criteria SC-001–SC-006 objectively measurable (percentages, single-command bring-up, exposure checks)? [Measurability, Spec §SC-001–SC-006]
- [x] CHK018 - Is "all components healthy" tied to a concrete, checkable signal (compose healthcheck / `curl /health`)? [Measurability, contracts/health.md, quickstart.md]

## Scenario & Edge Case Coverage

- [x] CHK019 - Is fail-fast behavior specified for a missing required environment variable? [Coverage, Spec Edge Cases, contracts/environment.md §Rules]
- [x] CHK020 - Is startup-ordering / "Postgres not yet accepting connections" behavior specified? [Coverage, Spec Edge Cases, contracts/service-topology.md §Boot ordering]
- [x] CHK021 - Is unauthenticated-Scrapyd-request handling specified (rejected, 401)? [Coverage, Spec §FR-012, contracts/service-topology.md §Scrapyd auth]
- [x] CHK022 - Is IPv6-only internal-network handling (dual-stack bind) specified for the components that must be internally reachable? [Coverage, Spec §FR-016, contracts/service-topology.md]
- [x] CHK023 - Is the dependency-boundary violation scenario (`app_shared` importing a scraping lib) covered by an enforceable requirement/test? [Coverage, Spec §FR-003, plan.md tests]

## Scope Discipline & Boundaries

- [x] CHK024 - Is out-of-scope explicitly declared (no DB schema/models/migrations, no auth/RLS, no scraping behavior; `alembic/` scaffolded empty)? [Boundary, Spec Assumptions, plan.md Summary]
- [x] CHK025 - Is the direct-connect one-shot migration job explicitly deferred (the sole future exception to PgBouncer-only routing)? [Boundary, Spec §FR-011, contracts/service-topology.md]

## Dependencies & Assumptions

- [x] CHK026 - Are the skeleton-stage assumptions (single Redis instance, docker-compose orchestration, platform DNS via env, port conventions) documented and validated against the master doc? [Assumption, Spec Assumptions, Clarifications]

## Notes

- All 26 items evaluated against spec.md, plan.md, and the three contracts. **26/26 pass.** No requirement gaps, ambiguities, or conflicts surfaced that required amending an artifact.
- ≥80% traceability: every item references a spec section, contract, or plan artifact.
- This checklist validates requirement *quality*; runtime verification of the eight components is covered by quickstart.md and the implement/converge phases.
