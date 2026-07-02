# Feature Specification: Monorepo & Services Skeleton

**Feature Branch**: `001-monorepo-skeleton`

**Created**: 2026-07-02

**Status**: Draft

**Input**: SPEC-01 from PROJECT_SPEC.md §35 — create the repo and service structure only; no business logic beyond health checks.

## Clarifications

### Session 2026-07-02

All items below were resolved directly from the master specification (`PROJECT_SPEC.md`); no open ambiguities required a stakeholder decision.

- Q: What packaging/build model does the monorepo use? → A: A `uv` workspace with one root project definition and one lockfile; each service image installs only its own member's dependency closure, including the future migration job (source: doc §3 Packaging, §5).
- Q: How is the local stack orchestrated? → A: A container-compose workflow (`docker-compose.yml` at the repo root) with the same images used in deployed environments (source: doc §5).
- Q: What is the API health endpoint and how is its port set? → A: `GET /health`, bound on the port supplied by the environment (example port 8000) (source: doc §4 api-service, §35 acceptance).
- Q: How does Postgres connectivity work at the skeleton stage? → A: All application services connect through PgBouncer (transaction-pooling mode, port 6432); no direct Postgres connections; the direct-connect migration job is out of scope here (source: doc §4 pgbouncer, §6).
- Q: One Redis instance or two at the skeleton stage? → A: A single local Redis instance is acceptable now; the split broker vs. locks/limits instances with `noeviction` are a deployment concern deferred to later specs (source: doc §4 redis).
- Q: Exact Python version and exact pinned infrastructure image tags? → A: Deferred to `/speckit-plan` as an implementation detail; the plan selects the current stable Python and pins concrete, current image versions (no `latest`) supported by the full stack (source: doc §3 Deployment "Pinned image versions", §4 hardening — version choice not enumerated in doc).

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are the platform's operators and developers who deploy and run the Crawmatic backend, plus the internal services that must reach one another. This feature delivers the empty but bootable skeleton on which every later spec is built.

### User Story 1 - Bring the whole stack up locally (Priority: P1)

A developer clones the monorepo, runs a single orchestration command, and every service and its backing infrastructure starts and stays healthy, so they have a working local platform to build features on.

**Why this priority**: Nothing else in the roadmap can be built or tested until the full multi-service stack boots reliably. This is the foundational MVP slice.

**Independent Test**: Run the local orchestration for the whole stack and confirm each of the eight services reaches a running/healthy state and that the API health endpoint responds successfully.

**Acceptance Scenarios**:

1. **Given** a clean checkout of the monorepo, **When** the developer starts the local orchestration, **Then** the API, scheduler, worker, Scrapyd HTTP, and Scrapyd browser services and the Postgres, PgBouncer, and Redis infrastructure all start without error.
2. **Given** the stack is running, **When** a client issues a health check to the API service, **Then** it returns a successful response indicating the service is up.
3. **Given** the stack is running, **When** each application service establishes its database connection, **Then** the connection is made through the connection pooler and never directly to the database.

### User Story 2 - Reach each service on its expected boundary (Priority: P1)

An operator verifies that only the API service is publicly reachable while the two Scrapyd nodes and other internal services are reachable only on the internal network, so the deployment matches the required security boundary.

**Why this priority**: The public/internal boundary is a non-negotiable security principle; getting it wrong at the skeleton stage propagates into every later spec.

**Independent Test**: From the internal network, confirm both Scrapyd nodes accept an authenticated request; confirm the API is reachable at its public boundary; confirm Scrapyd nodes are not exposed publicly.

**Acceptance Scenarios**:

1. **Given** the stack is running, **When** an authorized internal caller queries the Scrapyd HTTP node, **Then** it responds and requires valid credentials.
2. **Given** the stack is running, **When** an authorized internal caller queries the Scrapyd browser node, **Then** it responds and requires valid credentials.
3. **Given** the stack is running, **When** a request without valid Scrapyd credentials is made to either node, **Then** the request is rejected.
4. **Given** the deployment topology, **When** the public surface is inspected, **Then** only the API service is publicly exposed and the Scrapyd nodes and internal services are not.

### User Story 3 - Configure services from the environment (Priority: P2)

An operator supplies configuration (database URL, Redis URL, Scrapyd node URLs, ports, credentials) through the environment, and every service reads its settings from that environment at startup, so the same images run across local and deployed environments without code changes.

**Why this priority**: Environment-driven configuration is required for the services to be portable, but it builds on the bootable skeleton rather than preceding it.

**Independent Test**: Provide a complete environment file, start the stack, and confirm each service picks up its configured values (ports, pooler host, Redis, Scrapyd URLs) rather than hardcoded defaults.

**Acceptance Scenarios**:

1. **Given** a provided environment configuration, **When** a service starts, **Then** it loads its connection targets and credentials from the environment.
2. **Given** the Scrapyd node URLs are provided as a comma-separated list, **When** the worker reads them, **Then** it treats them as a pool of one-or-more nodes per mode.
3. **Given** an example environment file is committed, **When** a new developer copies it, **Then** they can populate real values without guessing which variables exist.

### Edge Cases

- What happens when a required environment variable is missing at startup? The affected service must fail fast with a clear error rather than start in a half-configured state.
- What happens when Postgres is not yet accepting connections as other services start? Services depending on the database must wait for/retry the pooler rather than crash-loop unmanaged, and the one-shot migration path (added in a later spec) is the only component permitted to connect directly to Postgres.
- What happens when an unauthenticated caller reaches a Scrapyd node on the internal network? The node must reject it (basic auth required), because internal networking alone is not sufficient protection for Scrapyd's code-upload API.
- What happens on a platform whose internal network is IPv6-only? Services that must be reachable internally must bind dual-stack, not IPv4-only.
- What happens if a service tries to import scraping dependencies it should not have? The dependency boundaries must keep scraping libraries out of the API/scheduler/worker images.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The repository MUST be organized as a single monorepo containing distinct application members `apps/api`, `apps/scheduler`, `apps/workers`, `apps/scrapers` (Scrapyd HTTP), and `apps/scrapers-browser` (Scrapyd browser), plus shared library members `libs/shared` and `libs/scrape-core`.
- **FR-002**: The monorepo MUST be a single workspace with one root project definition and one lockfile, where each service installs only its own member's dependency closure.
- **FR-003**: The system MUST enforce the dependency boundaries: `libs/shared` MUST NOT import scraping/browser libraries (Scrapy/Twisted/Playwright); `libs/scrape-core` MAY depend on `libs/shared` but not the reverse; the API, scheduler, and worker images MUST NOT pull scraping dependencies.
- **FR-004**: The two Scrapy projects (HTTP and browser) MUST share their extraction, item model, validation, confidence, DB pipeline, and rate-limiter code via `libs/scrape-core`, differing only in download handler/browser settings and spider entrypoint. (Skeleton stage: the shared member exists and is imported; behavior is added later.)
- **FR-005**: The API service MUST expose a health endpoint that reports the service is up, and MUST contain no business logic beyond health checks at this stage.
- **FR-006**: The scheduler service MUST boot to a running state via its documented start command with no business logic beyond starting.
- **FR-007**: The worker service MUST boot to a running state via its documented start command with no business logic beyond starting.
- **FR-008**: The Scrapyd HTTP service MUST boot, be reachable on the internal network, and carry its Scrapy project at build time (no reliance on runtime spider uploads).
- **FR-009**: The Scrapyd browser service MUST boot, be reachable on the internal network, and include the browser runtime at build time, with low browser concurrency.
- **FR-010**: Postgres, PgBouncer, and Redis MUST run as part of the local stack.
- **FR-011**: Every application service (API, scheduler, worker, both Scrapyd nodes) MUST connect to Postgres through PgBouncer and MUST NOT connect to Postgres directly. (The one-shot migration job that connects directly is out of scope for this spec and introduced later.)
- **FR-012**: Both Scrapyd nodes MUST require basic authentication, and any component that will call Scrapyd MUST be able to authenticate; unauthenticated requests MUST be rejected.
- **FR-013**: Only the API service MUST be publicly exposed; the Scrapyd nodes and other internal services MUST NOT be reachable from the public internet.
- **FR-014**: All infrastructure and base images used by the stack MUST be version-pinned (no floating/`latest` tags).
- **FR-015**: All service containers MUST run as a non-root user.
- **FR-016**: Services that must be reachable on internal networking (API, both Scrapyd nodes, PgBouncer) MUST bind dual-stack (IPv4 and IPv6).
- **FR-017**: Each service MUST load its configuration (connection targets, ports, credentials) from the environment at startup, and the repository MUST include an example environment file enumerating the required variables.
- **FR-018**: The worker's Scrapyd node URLs MUST be accepted as comma-separated lists (one entry per mode is allowed in v1) and treated as a pool.
- **FR-019**: Each service MUST have a documented, runnable start command.
- **FR-020**: The database engine/connection handling MUST be structured so that one engine is created per process, lazily on first use (never at import time, never per request), consistent with transaction-pooling operation. (Skeleton establishes the pattern; full DB models arrive later.)

### Key Entities *(include if feature involves data)*

- **Service member**: A deployable unit in the monorepo (api, scheduler, worker, scrapers, scrapers-browser) with its own start command, dependency closure, and network exposure classification (public vs internal).
- **Shared library member**: A non-deployable code package (`libs/shared`, `libs/scrape-core`) imported by service members under fixed dependency-direction rules.
- **Infrastructure component**: A backing service in the stack (Postgres, PgBouncer, Redis) that application services depend on, reached via defined boundaries (Postgres only through PgBouncer).
- **Environment configuration**: The set of environment variables (database URL, Redis URL, Scrapyd URLs, ports, credentials) that parameterize every service.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Starting the local stack brings all eight components (API, scheduler, worker, Scrapyd HTTP, Scrapyd browser, Postgres, PgBouncer, Redis) to a running/healthy state with zero manual intervention beyond the single start command.
- **SC-002**: The API health check returns a successful response 100% of the time once the API service is up.
- **SC-003**: 100% of application-service database connections are established through PgBouncer; zero direct-to-Postgres connections occur from application services.
- **SC-004**: Both Scrapyd nodes reject 100% of requests that lack valid credentials and accept authenticated requests from the internal network.
- **SC-005**: Only the API service is reachable on the public boundary; the Scrapyd nodes and other internal services are unreachable publicly in 100% of exposure checks.
- **SC-006**: A new developer can bring the full stack up from a clean checkout using only the committed instructions and example environment file, without reading source code.

## Assumptions

- Local orchestration is provided via a container-compose workflow; deployed environments use the same images with environment-specific configuration (per PROJECT_SPEC §4–§6).
- Local development may use relaxed pooler authentication (`trust`) while every deployed environment uses `scram-sha-256` with a real userlist; only the skeleton and local wiring are in scope here.
- Redis may run as a single local instance for the skeleton; the split broker/locks instances with `noeviction` are a deployment concern refined in later specs.
- No database schema, migrations, models, or scraping/business behavior are in scope for this spec — only the bootable structure and health checks.
- The specific hosting platform's internal DNS names (e.g. `*.railway.internal`) are configured via environment variables and are not hardcoded.
- Ports follow PROJECT_SPEC conventions (API app port, Scrapyd 6800, PgBouncer 6432) unless overridden by the environment.
