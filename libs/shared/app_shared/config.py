"""Environment-driven settings for every application member.

Enumerates every variable declared in
``specs/001-monorepo-skeleton/contracts/environment.md``. Required
variables have no default: a missing value raises
``pydantic.ValidationError`` at construction time, so a misconfigured
service fails fast and loudly instead of starting half-configured
(FR-017). Optional/derived variables (base URLs, pool sizing) carry
sensible defaults.

Use :func:`get_settings` to obtain the process-wide cached instance;
avoid constructing ``Settings()`` directly outside of tests so
configuration is parsed exactly once per process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_pool(value: str) -> list[str]:
    """Split a comma-separated URL pool, stripping whitespace/empties.

    Treated as a pool even when it contains a single URL (FR-018).
    """
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_encryption_keys(value: str) -> dict[int, str]:
    """Parse ``"version:key,version:key"`` into a ``{version: key}`` keyring.

    Per contracts/encryption.md (SPEC-10 FR-003, §33): ``ENCRYPTION_KEYS`` is a
    comma-separated list of ``version:key`` pairs, ``key`` a urlsafe-base64
    Fernet key. Raises ``ValueError`` on a malformed pair or a non-integer
    version so a misconfigured deployment fails fast at ``Settings``
    construction rather than at first encrypt/decrypt call.
    """
    keyring: dict[int, str] = {}
    for pair in value.split(","):
        pair = pair.strip()
        if not pair:
            continue
        version_str, sep, key = pair.partition(":")
        if not sep or not key:
            raise ValueError(
                f"malformed ENCRYPTION_KEYS pair {pair!r} (expected 'version:key')"
            )
        try:
            version = int(version_str.strip())
        except ValueError as exc:
            raise ValueError(
                f"malformed ENCRYPTION_KEYS version {version_str!r} (expected an integer)"
            ) from exc
        keyring[version] = key.strip()
    return keyring


class Settings(BaseSettings):
    """Process-wide configuration sourced from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database (required — never silently defaulted) ---
    # Host must be pgbouncer:6432, never postgres:5432 (FR-011).
    DATABASE_URL: str
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 2

    # --- Migration job (optional — direct-to-Postgres, bypasses the pooler) ---
    # Used ONLY by the one-shot migration job / Alembic env.py (host must be
    # postgres:5432, never pgbouncer:6432 — advisory locks and concurrent
    # index builds are unsafe under transaction pooling). App services keep
    # using DATABASE_URL (pooler) and never set/need this.
    MIGRATION_DATABASE_URL: str | None = None

    # --- Redis (required) ---
    REDIS_URL: str

    # --- Redis server-policy assertion (audit 2026-08-15 risk H2) ---
    # This Redis holds correctness/cost-critical keys (match locks,
    # dispatch sentinels, `proxybudget:*` spend counters) alongside the
    # Celery broker, so an eviction-capable `maxmemory-policy` can
    # silently duplicate paid work and erase the spend ledger at once.
    # When true (default), a process that CONFIRMS a non-`noeviction`
    # policy refuses to start. A server that cannot answer `CONFIG GET`
    # (managed/ACL-blocked/fakeredis) is only warned about -- never
    # fatal -- which is why defaulting this on is safe for local dev and
    # the test suite. Set false to override during an incident.
    # See `app_shared.redis_policy`.
    PROXY_REDIS_REQUIRE_NOEVICTION: bool = True

    # --- Paid-proxy fail-closed posture (audit 2026-08-15 risk H3) ---
    # EMERGENCY OVERRIDE, default OFF. When the cost ledger (Redis) is
    # unavailable, NEW PROXIED (paid) requests are denied while DIRECT
    # (unpaid) requests continue -- a Redis incident must not remove the
    # cost brake (the 2026-08-12 hostname-normalisation loop would have
    # spent ~$325/month unattended). Setting this true restores the old
    # fail-OPEN behaviour so an operator can keep proxied scraping alive
    # during a Redis outage at knowing financial risk. API request rate
    # limiting is unaffected and remains fail-open by design.
    # See `app_shared.access.budget`.
    PROXY_LEDGER_FAIL_OPEN: bool = False

    # --- Independent spend circuit breaker (audit 2026-08-15 risk H3) ---
    # Authoritative state lives in Postgres (`proxy_circuit_breakers`),
    # NOT Redis -- a Redis-backed counter cannot protect against Redis
    # failure. Thresholds are evaluated against the durable
    # `request_attempts` / `strategy_discovery_runs` audit tables. Set
    # any limit to None to disable that single trip condition; set
    # PROXY_BREAKER_ENABLED=false to disable the breaker entirely.
    PROXY_BREAKER_ENABLED: bool = True
    #: Hard ceiling on month-to-date proxied requests (absolute spend).
    PROXY_BREAKER_MONTHLY_PROXIED_REQUESTS: int | None = 250_000
    #: Month-end forecast from 1h/24h velocity may not exceed the
    #: absolute monthly ceiling by more than this factor.
    PROXY_BREAKER_VELOCITY_FACTOR: float = 1.5
    #: Minimum samples before a velocity window is trusted (a 3-request
    #: hour must not extrapolate into a trip).
    PROXY_BREAKER_VELOCITY_MIN_SAMPLE: int = 200
    #: Max proxied requests per DISTINCT url in the trailing 24h. The
    #: 2026-08-10 measurement was amazon 2,716 fetches / 1,097 urls =
    #: 2.48, so 8 is comfortably above healthy retry behaviour and well
    #: below a rediscovery loop.
    PROXY_BREAKER_MAX_REQUESTS_PER_URL: float | None = 8.0
    PROXY_BREAKER_REQUESTS_PER_URL_MIN_SAMPLE: int = 500
    #: Max strategy discovery runs per domain per day.
    PROXY_BREAKER_MAX_DISCOVERY_RUNS_PER_DOMAIN_PER_DAY: int | None = 50
    #: How often any one process re-evaluates the durable breaker.
    PROXY_BREAKER_EVAL_INTERVAL_SECONDS: int = 300
    #: How long a process may reuse its cached breaker verdict before
    #: re-reading the durable row. Bounds how long a trip takes to stop
    #: paid work fleet-wide.
    PROXY_BREAKER_STATE_CACHE_SECONDS: int = 30

    # --- Scrapyd pools & auth (required) ---
    # NoDecode: env values are plain comma-separated strings, not JSON —
    # skip pydantic-settings' default JSON decoding for complex types and
    # let the validator below split them.
    SCRAPYD_HTTP_URLS: Annotated[list[str], NoDecode]
    SCRAPYD_BROWSER_URLS: Annotated[list[str], NoDecode]
    SCRAPYD_USERNAME: str
    SCRAPYD_PASSWORD: str

    # --- API surface ---
    # API_PORT is the single canonical API-port variable; compose derives
    # the container's uvicorn $PORT from it. Not a required var — 8000
    # is a safe local default.
    API_PORT: int = 8000
    API_PUBLIC_BASE_URL: str | None = None
    INTERNAL_API_BASE_URL: str | None = None

    # --- Auth / JWT (required — never silently defaulted, SPEC-03 FR-024) ---
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_SECONDS: int = 900
    REFRESH_TOKEN_TTL_SECONDS: int = 2592000

    # --- SaaS control-plane service auth (PLAN §7.1) ---
    # Static bearer token the SaaS control plane presents to
    # `/v1/admin/*`. Machine analog of SUPER_ADMIN: it resolves no
    # workspace context and is compared in constant time. Optional so an
    # engine deployment that hosts no SaaS control plane still boots —
    # when unset, every admin request is refused (fail-closed).
    SAAS_SERVICE_TOKEN: str | None = None

    # --- Status cache (SPEC-03 FR-022) ---
    STATUS_CACHE_TTL_SECONDS: int = 30

    # --- Login rate limiting (SPEC-03 FR-007) ---
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 60

    # --- API-key last-used throttle (SPEC-03 FR-015) ---
    API_KEY_LAST_USED_THROTTLE_SECONDS: int = 60

    # --- Scrape-profile resolution cache (SPEC-06 FR-019) ---
    PROFILE_RESOLUTION_CACHE_TTL_SECONDS: int = 30

    # --- Batched persistence flush knobs (SPEC-07 FR-017, Principle VIII) ---
    # The scraping runtime's batched persistence pipeline (consumed via
    # get_settings(), never imported the other way — app_shared MUST NOT
    # depend on the scraping-side library) flushes whichever of these
    # thresholds is reached first (+ a final flush at spider close) —
    # DB-tunable so a live deployment can retune without a code change.
    SCRAPE_FLUSH_MAX_ITEMS: int = 50
    SCRAPE_FLUSH_INTERVAL_SECONDS: float = 2.0

    # --- Auth DB role (optional — direct BYPASSRLS role for pre-auth
    # credential lookups only; see app_shared.database.get_auth_session).
    # Deliberately never falls back to DATABASE_URL (SPEC-03 [analyze C1]).
    AUTH_DATABASE_URL: str | None = None

    # --- Argon2id tuning (optional — argon2-cffi defaults apply when unset) ---
    ARGON2_TIME_COST: int | None = None
    ARGON2_MEMORY_COST: int | None = None
    ARGON2_PARALLELISM: int | None = None

    # --- Jobs & orchestration dispatch tuning (SPEC-08 FR-011, FR-015,
    # Principle IV — DB/env-tunable, never hardcoded literals) ---
    SCRAPE_DISPATCH_HTTP_BATCH_MIN: int = 50
    SCRAPE_DISPATCH_HTTP_BATCH_MAX: int = 200
    # Browser-mode batches need a much smaller ceiling than HTTP (audit
    # item M1, 5-25 guidance range) -- a browser Scrapyd run pays a
    # per-target headless-render cost HTTP does not, so the same 200-wide
    # chunk that's fine for HTTP makes one browser batch's wall time
    # enormous. plan_batches() branches on the group's mode to apply this
    # instead of SCRAPE_DISPATCH_HTTP_BATCH_MAX.
    SCRAPE_BATCH_BROWSER_MAX: int = 15
    SCRAPE_STALL_TIMEOUT_SECONDS: int = 900
    # TTL on the dispatch client's Redis idempotency guard
    # (``dispatched:{job}:{batch_index}``). Without an expiry the guard
    # suppressed every later re-dispatch of the same batch_index forever —
    # the second half of the DEFERRED deadlock (PLAN_AMAZON_NOON_PRICING
    # Phase 1). Doubles as the pacing floor: a still-deferred batch can
    # actually re-POST at most once per TTL window.
    SCRAPYD_DISPATCH_GUARD_TTL_SECONDS: int = 900
    # How many times one target may be handed back DEFERRED before it is
    # called a terminal FAILED (2026-08-03; consumed by the scraping
    # runtime's defer-budget helper). DEFERRED is non-terminal, so without
    # a bound a persistently blocked domain cycles forever and its job
    # never finalizes.
    SCRAPE_MAX_DEFER_CYCLES: int = 3

    # Domains whose fetches go through the scraping runtime's
    # Chrome-impersonating TLS transport (2026-08-05) instead of the
    # Scrapy/Twisted HTTP client. amazon.sa and noon.com reject that
    # client itself -- not our headers, not our IPs -- so header tuning
    # cannot fix them and a per-domain transport swap can. Matched by
    # host equality or dot-anchored suffix, which is why turning these
    # two sites on needs no spider change. Everything not listed keeps
    # the existing `HTTP11DownloadHandler` path exactly.
    SCRAPE_IMPERSONATE_DOMAINS: str = "amazon.sa,noon.com"
    # curl_cffi impersonation target. `chrome131` is the profile already
    # verified 2/2 against noon through the production proxy
    # (matching/NOON_PROXY_MATRIX_2026-08-02.md).
    SCRAPE_IMPERSONATE_PROFILE: str = "chrome131"

    # --- Celery worker sizing (PLAN_AMAZON_NOON_PRICING Phase 4a) ---
    # Prefork pool size for the `worker` service. Celery's default is one
    # process per CPU core, which on Railway meant ~48 idle processes
    # holding ~3.85 GB (~$31/mo of the ~$40 baseline) for a queue mix
    # that is entirely short DB/broker calls. Raise if `scrape_dispatch`
    # queue depth grows during a full run (under-provisioning shows up as
    # backlog, not errors).
    CELERY_WORKER_CONCURRENCY: int = 4

    # Prefork child recycling (2026-08-03 memory-leak hardening). A child
    # is retired after this many tasks, and after its resident set passes
    # CELERY_MAX_MEMORY_PER_CHILD_KB — Celery finishes the running task
    # first, then replaces the process, so recycling never interrupts or
    # throttles a legitimate heavy run; it only bounds how long a leak can
    # accumulate. 300 MB is several times the steady-state footprint of
    # this task mix (short DB/broker calls), so a healthy child is
    # recycled by task count, not by memory.
    CELERY_MAX_TASKS_PER_CHILD: int = 1000
    CELERY_MAX_MEMORY_PER_CHILD_KB: int = 300000

    # --- Celery delivery reliability (2026-08-15 audit risk H1) ---------
    #
    # Redis broker visibility timeout, in seconds: how long a delivered-
    # but-unacknowledged message stays invisible before the broker hands
    # it to another worker. It MUST exceed the longest task runtime, or a
    # still-running task is redelivered and executed twice.
    #
    # Measured worst case in this codebase is `STRATEGY_DISCOVERY_RUN`
    # (`apps/workers/app/workers/tasks_strategy.py`): its probe loop walks
    # `_ACCESS_LADDER` (DIRECT_HTTP, DIRECT_HTTP_RETRY = 2 requests,
    # PROXY_HTTP) over up to `STRATEGY_DISCOVERY_MAX_SAMPLE` = 10 URLs at
    # `_PROBE_TIMEOUT_SECONDS` = 15s each => (1 + 2 + 1) * 10 * 15s = 600s
    # of pure network wait, plus extraction. Every other task is a bounded
    # DB sweep or a single Scrapyd POST. 3600s is 6x that worst case, and
    # also covers a slow `MAINTENANCE_RETENTION_DROP`/`DAILY_ROLLUP` on a
    # large month. The cost of the generous margin is bounded: a hard
    # SIGKILL of a whole worker delays redelivery by up to an hour, but
    # `task_reject_on_worker_lost` already re-queues the common
    # child-death case immediately, and the scheduler's finalize /
    # recover-stalled / redispatch / outbox-drain passes re-drive anything
    # important on their own cadence.
    CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS: int = 3600

    # Messages a worker reserves per child process beyond the one it is
    # executing. 1 (Celery's "fair" setting) is chosen deliberately over
    # the default 4: with `task_acks_late` a prefetched message is held
    # unacknowledged, so a worker that dies strands everything it
    # prefetched for a full visibility timeout. At concurrency 4 that is
    # 4 stranded messages instead of 16. Prefetch batching buys nothing
    # here anyway — every task in this system runs for seconds to minutes
    # while a broker round-trip is ~1ms — and it actively hurts the
    # `scrape_dispatch` queue, where a hoarded message is a scrape that
    # is not running.
    CELERY_WORKER_PREFETCH_MULTIPLIER: int = 1

    # --- Transactional outbox (2026-08-15 audit risk H1) ---------------
    #
    # `OUTBOX_DRAIN_INTERVAL_SECONDS` is the scheduler cadence for the
    # drain pass; it is also the worst-case added latency between a
    # producer's COMMIT and its follow-up task reaching the broker, so it
    # is deliberately much tighter than the 60s maintenance tick.
    OUTBOX_DRAIN_INTERVAL_SECONDS: int = 5
    # Messages published per drain pass. Bounded so one pass cannot hold a
    # worker for an unbounded time; the next tick continues the backlog.
    OUTBOX_DRAIN_BATCH_LIMIT: int = 200
    # Publish attempts before a message becomes DEAD (alertable dead
    # letter). With the capped exponential backoff in
    # `app_shared.outbox.dispatcher` this spans well over an hour of
    # broker unavailability before anything is given up on.
    OUTBOX_MAX_ATTEMPTS: int = 8
    # First-retry delay after a failed publish; doubles per attempt up to
    # `dispatcher.MAX_BACKOFF_SECONDS`.
    OUTBOX_RETRY_BACKOFF_BASE_SECONDS: int = 10
    # Reconciliation/retention cadence + the age at which a still-PENDING
    # message counts as "stuck" for alerting.
    OUTBOX_RECONCILE_INTERVAL_SECONDS: int = 300
    OUTBOX_STUCK_AFTER_SECONDS: int = 900
    # How long a PUBLISHED row is kept before deletion (DEAD rows are kept
    # `DEAD_RETENTION_MULTIPLIER` times longer — they are incident
    # evidence). The table is a drain-to-empty queue, not a history table,
    # so this is short by design.
    RETENTION_OUTBOX_MESSAGES_DAYS: int = 7

    # --- Scrapy MemoryUsage extension (2026-08-03 memory-leak hardening,
    # Principle IV — env-tunable, never a hardcoded literal in the Scrapy
    # settings modules). Bounds a single *spider* process on both Scrapyd
    # nodes: at the warning mark it only logs, at the limit Scrapy closes
    # the spider gracefully (finishing in-flight items through the
    # persistence pipeline) instead of letting the process grow until the
    # container OOMs. The container-wide backstop for the long-lived
    # Scrapyd/Celery *parents* is app_shared.memory_watchdog, driven by
    # WATCHDOG_MEMORY_LIMIT_MB (an ops/Railway env knob, not a Setting).
    #
    # 1024 MB is well above a real ~3k-product run's spider footprint, so
    # legitimate heavy runs are never throttled by it. ---
    SCRAPE_MEMUSAGE_LIMIT_MB: int = 1024
    SCRAPE_MEMUSAGE_WARNING_MB: int = 512
    SCRAPE_MEMUSAGE_CHECK_INTERVAL_SECONDS: int = 30

    # --- Price-analysis recompute dedup (SPEC-09 FR-012, FR-015, D4, D7 —
    # DB/env-tunable, never a hardcoded literal, Principle IV). TTL on the
    # emission-side Redis ``SET NX`` key (``analysis:enqueued:{job}:{variant}``)
    # — comfortably longer than a single job's lifetime so late-arriving
    # completions of the same job still dedup. The ``price_analysis`` queue
    # name itself is a code constant in ``celery_app.py``, not config. ---
    PRICE_ANALYSIS_DEDUP_TTL_SECONDS: int = 21600

    # --- Secret encryption (SPEC-10 FR-003, §33) ---
    # Comma-separated "version:key" pairs; key is a urlsafe-base64 Fernet key,
    # e.g. "1:kZ...=,2:9p...=". Required so a misconfigured deployment fails
    # fast (never falls back to a default/plaintext key).
    ENCRYPTION_KEYS: str
    ENCRYPTION_PRIMARY_KEY_VERSION: int = 1

    # --- Access-policy resolution cache (SPEC-10 FR-010/FR-011, §9/§22) ---
    # Ceiling/cooldown values are per-policy/per-domain DB columns, not
    # global settings — only the resolution-cache TTL lives here.
    ACCESS_RESOLUTION_CACHE_TTL_SECONDS: int = 30

    # --- Distributed rate limiting & in-flight match locks (SPEC-11,
    # data-model.md §4, Principle IV — env-tunable, never a hardcoded
    # literal). Per-domain/per-rule overrides still win via
    # `DomainAccessRule`/`AccessPolicy` (app_shared.limiter.limits); these
    # are only the built-in defaults + lock/backoff/requeue knobs. ---
    RATE_LIMIT_DEFAULT_PER_MINUTE: int = 60
    RATE_LIMIT_DEFAULT_CONCURRENCY: int = 4
    RATE_LIMIT_KEY_TTL_SLACK_SECONDS: int = 120
    SEMAPHORE_SLOT_TTL_SECONDS: int = 600
    MATCH_LOCK_HTTP_TTL_SECONDS: int = 600
    MATCH_LOCK_BROWSER_TTL_SECONDS: int = 1800
    REQUEUE_MAX_ATTEMPTS: int = 5
    REQUEUE_MAX_TOTAL_WAIT_SECONDS: int = 300
    RATE_LIMIT_JITTER_MIN_SECONDS: int = 2
    RATE_LIMIT_JITTER_MAX_SECONDS: int = 20

    # --- Same-URL fetch dedup (2026-08-11 proxy-cost reduction, Fix 1) ---
    # When enabled, the HTTP spider folds same-job targets that resolve to
    # the identical (competitor, exact URL, profile, policy) fetch into one
    # request and fans the fetched result out to every sibling match --
    # measured Aug-10: amazon fetched 2,716 times for 1,097 distinct URLs.
    # Off by default: flipped per-environment after the A/B verification
    # run (PLAN_PROXY_COST_REDUCTION.md). Grouping is refused for targets
    # whose profile is variant-aware (non-PAGE_SINGLE_PRICE or a
    # variant_selector_config) -- see `group_targets_for_dedup` in the
    # scraper-side targets module.
    SCRAPE_URL_DEDUP: bool = False

    # --- Domain strategy optimizer tuning (SPEC-12, data-model §7 /
    # research D11, Principle IV — env-tunable, never a hardcoded
    # literal). Promotion/rediscovery/discovery thresholds are global
    # defaults; per-domain overrides go through `DomainAccessRule`, not
    # here. ---
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD: float = 0.85
    STRATEGY_PROMOTION_MIN_SUCCESSES: int = 3
    STRATEGY_PROMOTION_MIN_DISTINCT_URLS: int = 3
    STRATEGY_REDISCOVERY_SUCCESS_RATE_FLOOR: float = 0.80
    STRATEGY_REDISCOVERY_LOW_CONFIDENCE: float = 0.75
    STRATEGY_REDISCOVERY_CONSECUTIVE_FAILURES: int = 3
    STRATEGY_DISCOVERY_MIN_SAMPLE: int = 3
    STRATEGY_DISCOVERY_MAX_SAMPLE: int = 10
    STRATEGY_STATS_FLUSH_INTERVAL_SECONDS: int = 60
    STRATEGY_STATS_KEY_TTL_SECONDS: int = 3600

    # --- Rediscovery/discovery local rate bounds (2026-08-15 runaway
    # backstop). These are NOT tuning knobs for how eagerly the optimizer
    # re-learns; they are the structural ceiling that keeps ANY future
    # correctness bug in the trigger conditions from authorising unbounded
    # discovery. `STRATEGY_LIGHT_RECHECK` fires every 60s, so a trigger
    # condition that its own remedy cannot clear costs 1,440 discovery
    # runs/profile/day (measured: fqtoners.com, 13,151 runs over 10 days).
    #
    # `STRATEGY_REDISCOVERY_MIN_INTERVAL_SECONDS` is the per-profile
    # cooldown enforced inside `apply_rediscovery`'s guarded UPDATE (so it
    # is a database predicate, not an app-side check that two concurrent
    # evaluations could both pass). `STRATEGY_DISCOVERY_MAX_RUNS_PER_KEY_PER_DAY`
    # is the independent per-(competitor, url_pattern) ceiling enforced in
    # `run_discovery` itself, so it bounds EVERY enqueue path -- including
    # any future one that never goes through `apply_rediscovery`.
    #
    # These complement `proxy_circuit_breakers` rather than duplicating
    # it: the breaker is a fleet-wide, manually-reset kill switch that
    # trips AFTER a domain has already burned its daily allowance
    # (`DISCOVERY_RUNS_PER_DOMAIN`); these are local, self-releasing
    # bounds that stop the burn at a handful of runs per key per day and
    # need no operator intervention. 0 disables either bound.
    STRATEGY_REDISCOVERY_MIN_INTERVAL_SECONDS: int = 21600  # 6h -> <=4 runs/profile/day
    STRATEGY_DISCOVERY_MAX_RUNS_PER_KEY_PER_DAY: int = 6

    # --- Strategy profile lookup-key scope (2026-07-11 domain-scope fix) ---
    # "domain": the profile/discovery lookup key is the bare competitor
    # domain (caps discovery cost at O(#competitors), fixes the discovery
    # gate never firing for per-product-slug catalogs). "url_pattern":
    # legacy exact-current behavior, kept as a config-only rollback (no
    # deploy needed). `derive_url_pattern`/`URL_PATTERN_ALGORITHM_VERSION`
    # are unchanged either way -- matches still stamp `url_pattern` so
    # pattern-level keying can be re-enabled later, data-driven.
    STRATEGY_PROFILE_SCOPE: str = "domain"

    # --- Scheduler refresh-pass tuning (SPEC-13 US2, research R8,
    # Principle IV — env-tunable, never a hardcoded literal). Poll
    # cadence and per-pass claim ceiling for `apps/scheduler`'s due-rule
    # loop (`app.scheduler.refresh.run_refresh_pass`). ---
    SCHEDULER_POLL_INTERVAL_SECONDS: int = 30
    SCHEDULER_CLAIM_BATCH_LIMIT: int = 100

    # --- Scheduler system DB role (optional — BYPASSRLS role for the
    # scheduler's inherently cross-tenant due-rule claim only; see
    # app_shared.database.get_system_session). Falls back to
    # AUTH_DATABASE_URL when unset (research R2) — unlike
    # AUTH_DATABASE_URL itself, which deliberately never falls back to
    # DATABASE_URL (SPEC-03 [analyze C1]).
    SYSTEM_DATABASE_URL: str | None = None

    # --- Browser scraping service tuning (SPEC-14, data-model.md §4,
    # Principle IV — env/DB-tunable, never a hardcoded literal). Default
    # wait/nav bound when a profile's `browser_timeout_ms` is unset (R10),
    # plus the browser Scrapyd project's deliberately low
    # CONCURRENT_REQUESTS / PLAYWRIGHT_MAX_CONTEXTS (R9). Reused unchanged:
    # MATCH_LOCK_BROWSER_TTL_SECONDS, SCRAPE_FLUSH_*, SCRAPYD_BROWSER_URLS. ---
    SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS: int = 30000
    BROWSER_CONCURRENT_REQUESTS: int = 2
    BROWSER_MAX_CONTEXTS: int = 1

    # --- Retention, rollups & partition maintenance tuning (SPEC-15,
    # data-model.md §6, Principle IV — env/DB-tunable, never a hardcoded
    # literal). Five per-table retention windows, three maintenance-task
    # cadence intervals, and the partition create-ahead lookahead. Reused
    # unchanged: SYSTEM_DATABASE_URL (-> AUTH_DATABASE_URL fallback) — no
    # new session knob added here. ---
    RETENTION_PRICE_OBSERVATIONS_DAYS: int = 90
    RETENTION_REQUEST_ATTEMPTS_DAYS: int = 90
    RETENTION_PRICE_ALERT_EVENTS_DAYS: int = 365
    RETENTION_WEBHOOK_EVENTS_DAYS: int = 90
    RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS: int = 730
    PARTITION_CREATE_INTERVAL_SECONDS: int = 86400
    DAILY_ROLLUP_INTERVAL_SECONDS: int = 86400
    RETENTION_INTERVAL_SECONDS: int = 86400
    # Raised 1 -> 3 in the 2026-08-15 readiness cycle. With a lookahead of
    # 1 the entire safety margin between "maintenance stops working" and
    # "every INSERT into four partitioned tables fails" is however many
    # days are left in the current month -- which is exactly how a silent
    # failure became a dated outage 17 days out. 3 months means the whole
    # maintenance path can be dead for ~90 days without a write outage,
    # which comfortably exceeds any plausible detect-and-fix window, at a
    # cost of at most 8 extra empty partitions across the registry (an
    # empty partition is a catalog entry and an empty heap -- no rows, no
    # measurable planning cost at this table count).
    PARTITION_CREATE_LOOKAHEAD_MONTHS: int = 3

    # --- Maintenance cadence durability + health assertions (2026-08-15
    # readiness cycle). `MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS` is how
    # often the scheduler ASKS the database whether a daily cadence is due
    # -- the deadline itself lives in `maintenance_cadences`, so this knob
    # only bounds detection latency after a restart, never the cadence.
    # `MAINTENANCE_HEALTH_*` drive the outcome assertions in
    # `app_shared.maintenance.health` (partition present? rollup fresh?),
    # which are what actually catches a maintenance task that is being
    # enqueued but failing downstream. ---
    MAINTENANCE_CADENCE_POLL_INTERVAL_SECONDS: int = 60
    MAINTENANCE_HEALTH_INTERVAL_SECONDS: int = 3600
    #: How far past its deadline a cadence may sit before
    #: `maintenance_cadence_overdue` fires. Two poll intervals of slack so
    #: a momentarily unreachable database does not page anyone.
    MAINTENANCE_CADENCE_OVERDUE_GRACE_SECONDS: int = 7200
    #: The daily rollup targets *yesterday*, so a healthy system is always
    #: ~1 day behind; 3 days tolerates a missed run plus a retry without
    #: masking a real stall.
    MAINTENANCE_ROLLUP_STALE_AFTER_DAYS: int = 3

    # --- Ops snapshot cadence (audit §H5) ---
    # How often the scheduler collects `app_shared.opsmetrics.snapshot`
    # and emits it plus every firing alert rule
    # (`app_shared.opsmetrics.emit`). Until this existed the rules were
    # only ever evaluated when a human happened to curl `GET /ops/metrics`,
    # so "alerting" meant "someone was already looking" -- which is not
    # alerting. 15 minutes is the compromise between detection latency on
    # the slow-moving conditions the rules actually cover (queue age,
    # spend velocity, partition/rollup health, outbox backlog, breaker
    # posture) and the cost of the collector, which is the heaviest read
    # pass in the scheduler: 24h and 7d aggregates across
    # `request_attempts`/`price_observations`.
    OPS_SNAPSHOT_INTERVAL_SECONDS: int = 900

    # --- Public API rate limits (PLAN §7.4, risk P5) ---
    # Per-credential fixed-window budgets. Reads are cheap and get the
    # generous budget; writes touch the catalog and get a tenth of it.
    # Tunable per deployment because a plugin doing a bulk catalog sync
    # has a legitimately different shape from a dashboard.
    API_RATE_LIMIT_ENABLED: bool = True
    API_RATE_LIMIT_READ_PER_MINUTE: int = 60
    API_RATE_LIMIT_WRITE_PER_MINUTE: int = 10

    @field_validator("SCRAPYD_HTTP_URLS", "SCRAPYD_BROWSER_URLS", mode="before")
    @classmethod
    def _parse_url_pool(cls, value: object) -> object:
        if isinstance(value, str):
            return _split_pool(value)
        return value

    @model_validator(mode="after")
    def _validate_encryption_keyring(self) -> "Settings":
        keyring = _parse_encryption_keys(self.ENCRYPTION_KEYS)
        if self.ENCRYPTION_PRIMARY_KEY_VERSION not in keyring:
            raise ValueError(
                "ENCRYPTION_PRIMARY_KEY_VERSION "
                f"{self.ENCRYPTION_PRIMARY_KEY_VERSION} not present in ENCRYPTION_KEYS"
            )
        return self

    @field_validator("STRATEGY_PROFILE_SCOPE")
    @classmethod
    def _validate_strategy_profile_scope(cls, value: str) -> str:
        if value not in ("domain", "url_pattern"):
            raise ValueError(
                f"STRATEGY_PROFILE_SCOPE must be 'domain' or 'url_pattern', got {value!r}"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Parsed once per process on first call; subsequent calls return the
    cached instance.
    """
    return Settings()
