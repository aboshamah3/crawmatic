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
from pathlib import Path
from typing import Annotated, Literal

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
    # Raised 5→8 / 2→4 (H2, production-readiness audit): pgbouncer sits in
    # front of Postgres and absorbs the extra backend connections, so these
    # are sized to match `API_THREAD_POOL_SIZE` below rather than Postgres's
    # own connection budget — a request thread pool bigger than the DB pool
    # just queues at the DB instead of the thread pool, moving the bottleneck
    # without fixing it.
    DB_POOL_SIZE: int = 8
    DB_MAX_OVERFLOW: int = 4

    # --- API request thread pool (H2) ---
    # `anyio.to_thread` runs every sync DB call (SQLAlchemy) off a worker
    # thread; if that pool is sized larger than DB_POOL_SIZE+DB_MAX_OVERFLOW,
    # requests can pile up waiting on DB connections while still holding a
    # thread-pool slot. Keep this <= DB_POOL_SIZE + DB_MAX_OVERFLOW.
    API_THREAD_POOL_SIZE: int = 8

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
    #: EPA B1: how long an OPEN breaker must stay open before an
    #: evaluation whose CURRENT window is clean may close it. `0` disables
    #: auto-recovery entirely (operator-only, the pre-B1 behaviour).
    #: Both halves are required, so a runaway that is still running keeps
    #: re-tripping and can never auto-close — what this recovers from is a
    #: trip whose cause is genuinely gone, which would otherwise have
    #: stopped ALL paid work until a human noticed.
    PROXY_BREAKER_AUTO_CLOSE_AFTER_SECONDS: int = 3600
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

    # --- Customer-supplied regex execution bounds (A2/F02) ---
    # `price_regex`/`old_price_regex`/`currency_regex`/`stock_regex` are
    # DB-supplied, learned, competitor-page-influenced text. They run on the
    # live scraping path through `regex.compile(...).search(..., timeout=)`
    # (the `regex` package — CPython's stdlib `re` cannot be interrupted
    # mid-match), so a catastrophic backtrack costs a bounded slice of one
    # page instead of pinning a scraper core forever.
    #: Per-pattern, per-node wall-clock deadline. The whole-page budget the
    #: extraction module applies is 4x this value across every text node.
    EXTRACTION_REGEX_TIMEOUT_SECONDS: float = 0.25
    #: Write-time cap on a profile regex's source length (validation refuses
    #: anything longer before it is ever compiled).
    EXTRACTION_REGEX_MAX_PATTERN_CHARS: int = 512
    #: Consecutive-window REGEX_TIMEOUT count (per rolling 24 h) after which a
    #: scrape profile's regex strategy is quarantined.
    EXTRACTION_REGEX_QUARANTINE_AFTER: int = 3
    # Promoted from module constants in the scraping-side library's regex
    # extraction module (named indirectly: this package must not reference
    # that library even in a comment - see
    # `tests/unit/test_import_boundaries.py`). They are the W3.2 pattern
    # pre-flight + input-truncation bounds, and they stay DEFAULT OFF: the
    # per-pattern execution deadline above is the primary containment now.
    EXTRACTION_REGEX_BOUNDS_ENABLED: bool = False
    #: Longest single text node any bounded pattern may see.
    EXTRACTION_REGEX_BOUNDS_MAX_NODE_CHARS: int = 65_536
    #: Total characters one bounded pattern may scan across every node.
    EXTRACTION_REGEX_BOUNDS_MAX_TOTAL_CHARS: int = 1_048_576

    # --- Ranked-extraction rollout flag (EPA C5, F19) -------------------
    # The W3.2 ranked path (`collect_extraction_candidates` ->
    # `rank`) has existed, tested, since 2026-08-26 and has never
    # decided a live price: turning it on is a pricing-behaviour change,
    # not a refactor, so it stayed opt-in per call.
    #
    #   `off`     the historical first-hit chain, nothing else runs.
    #   `shadow`  the first hit is STILL what the caller gets and what
    #             gets persisted; the ranker also runs and every
    #             material disagreement is recorded as an
    #             `extraction_shadow_events` row. This is how the
    #             disagreement rate becomes a measured number instead of
    #             an argument.
    #   `v1`      the ranker decides. **An OWNER decision at C11**, taken
    #             only if the shadow disagreement rate is < 1% on the C4
    #             labeled sets AND the ranker wins >= 99% of labeled
    #             conflicts (`scripts/run_offer_benchmark.py
    #             --from-shadow-events`).
    #
    # `shadow` is the default because a rollout flag whose default is
    # `off` produces no evidence, and one whose default is `v1` changes
    # prices before anybody has any.
    EXTRACTION_RANKING_POLICY: Literal["off", "shadow", "v1"] = "shadow"

    # --- Raw-evidence blob store (EPA C5 / W3.1, F19) -------------------
    # Where `app_shared.observations.evidence_store` writes the bytes an
    # observation's `offer_raw_evidence_hash` names. In production this
    # is a Railway VOLUME mounted on both scraper services — the store is
    # content-addressed, so two services writing the same page write the
    # same path with the same content, and a replay from either resolves.
    #
    # `None` is not a default location, it is "evidence storage is not
    # configured": the pipeline then writes NO blob and leaves
    # `offer_raw_evidence_hash` NULL. That is deliberate. A container's
    # ephemeral filesystem would accept every write and lose them on the
    # next deploy, leaving a table full of content addresses that resolve
    # to nothing — exactly the "a hash pointing at deleted data proves
    # nothing" failure `docs/RETENTION_POLICY.md` §2.1 is about. A NULL
    # column is an honest absence; a dangling hash is a false claim.
    EVIDENCE_STORE_DIR: str | None = None
    #: How long a blob is kept once nothing needs it any more. The sweep
    #: (`MAINTENANCE_EVIDENCE_RETENTION`) is age-AND-reference gated: a
    #: blob past this window is still kept while any observation inside
    #: `RETENTION_PRICE_OBSERVATIONS_DAYS` still references its hash.
    EVIDENCE_RETENTION_DAYS: int = 30
    #: Cadence of that sweep. Daily, like every other retention job.
    EVIDENCE_RETENTION_INTERVAL_SECONDS: int = 86400
    #: Blobs one sweep run may delete. Bounds the run's wall clock and
    #: the number of reference queries it issues; a truncated run simply
    #: continues on the next tick (the report says `truncated=True`).
    EVIDENCE_RETENTION_MAX_BLOBS_PER_RUN: int = 5000

    # --- Batched persistence flush knobs (SPEC-07 FR-017, Principle VIII) ---
    # The scraping runtime's batched persistence pipeline (consumed via
    # get_settings(), never imported the other way — app_shared MUST NOT
    # depend on the scraping-side library) flushes whichever of these
    # thresholds is reached first (+ a final flush at spider close) —
    # DB-tunable so a live deployment can retune without a code change.
    SCRAPE_FLUSH_MAX_ITEMS: int = 50
    SCRAPE_FLUSH_INTERVAL_SECONDS: float = 2.0

    # --- Durable result spool (EPA F05, plan task B1) --------------------
    # The spider-side SQLite/WAL queue every scrape result is written to
    # BEFORE it enters the in-memory flush buffer, drained by the
    # scraping-core library's `result_spool.ResultSpool` (deliberately not
    # named in full here -- `tests/unit/test_import_boundaries.py` forbids
    # this package from mentioning that one at all, even in a comment, so
    # the reverse dependency edge cannot creep back in via a lazy import).
    # Placed alongside the network
    # ledger's own buffer (`NETLEDGER_BUFFER_PATH`, an env-only per-host
    # fact) so both durable queues live on the same volume and one mount
    # makes the host crash-safe rather than two.
    SCRAPE_RESULT_SPOOL_PATH: Path = Path("/var/lib/crawmatic/spool/scrape_results.sqlite3")
    #: How many flushes may be in flight before `process_item` starts
    #: returning an unfired Deferred. Scrapy honours that by stopping its
    #: pull from the scheduler, so the downloader stalls and admission
    #: pauses -- backpressure instead of an unbounded spool when Postgres
    #: is slower than the crawl.
    SCRAPE_FLUSH_MAX_PENDING_BATCHES: int = 8
    #: Delay before each successive replay of a failed batch, indexed by
    #: the batch's attempt count (the last entry repeats). Comma-separated
    #: in env (`"1,5,30,120,600"`), never JSON -- same convention as the
    #: Scrapyd URL pools.
    SCRAPE_FLUSH_RETRY_BACKOFF_SECONDS: Annotated[tuple[float, ...], NoDecode] = (
        1.0,
        5.0,
        30.0,
        120.0,
        600.0,
    )
    #: Failed attempts after which a spooled batch stops being retried and
    #: moves to `kind='quarantined'` in the spool -- kept on disk for an
    #: operator, never deleted (the fetch was already paid for).
    SCRAPE_FLUSH_QUARANTINE_AFTER: int = 5

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
    # --- EPA B3 (F07, 2026-09-07): work-level fairness in the redispatch
    # sweep. `redispatch_pending_jobs` used to walk `_scan_job_refs`'s
    # rows in whatever order Postgres returned them -- which, for a
    # backlogged tenant, is that tenant's jobs first and all of them. One
    # workspace with 400 wedged jobs therefore filled every re-dispatch
    # tick, and a second tenant's single stuck job waited behind the
    # whole backlog. The sweep now interleaves workspaces round-robin and
    # re-enqueues at most this many jobs per workspace per tick; the rest
    # of that workspace's backlog is picked up by the following tick, in
    # the same round-robin order.
    #
    # 4 rather than 1: a tick must still make real progress on a genuine
    # backlog (the sweep runs on the 60s maintenance cadence), and every
    # re-enqueue is idempotent and paced further downstream by the
    # dispatch client's Redis guard TTL. It is a fairness knob, not a
    # rate limit -- the rate limits live in the fleet/domain concurrency
    # caps and the cost gate.
    SCRAPE_DISPATCH_PER_WORKSPACE_BATCHES_PER_TICK: int = 4
    # --- EPA A3/B2 (2026-09-03): the two maintenance-sweep deadlines that
    # stop a job dangling RUNNING forever after a scrapyd container is
    # replaced mid-run. See `app_shared.jobs.reaper`.
    #
    # How long a target may sit STARTED before the reaper concludes the
    # spider that claimed it is gone and reverts it to PENDING.
    # `MATCH_LOCK_BROWSER_TTL_SECONDS` (1800) is the longest a healthy
    # in-flight target can legitimately hold its lock, so anything past
    # that + a 300s grace is provably unowned: nothing can be racing the
    # revert, because the claimant's own lock has already expired.
    SCRAPE_STARTED_REAP_AFTER_SECONDS: int = 2100
    # Hard ceiling on a single job's wall-clock runtime. Past it, every
    # non-terminal target is failed `JOB_DEADLINE_EXCEEDED` so
    # `finalize_jobs` can close the job. 12h is far beyond any legitimate
    # refresh (the largest measured full run is hours, not half a day),
    # so this is a wedge detector, not a throughput limit.
    SCRAPE_JOB_MAX_RUNTIME_SECONDS: int = 43200
    # TTL on the dispatch client's Redis idempotency guard
    # (``dispatched:{job}:{batch_index}``). Without an expiry the guard
    # suppressed every later re-dispatch of the same batch_index forever —
    # the second half of the DEFERRED deadlock (PLAN_AMAZON_NOON_PRICING
    # Phase 1). Doubles as the pacing floor: a still-deferred batch can
    # actually re-POST at most once per TTL window.
    SCRAPYD_DISPATCH_GUARD_TTL_SECONDS: int = 900
    # EPA B1: pass the dispatch intent's id to Scrapyd as ``schedule.json``'s
    # optional ``jobid`` field, so the remote run's identity equals our
    # durable intent's and an interrupted dispatch can be reconciled by
    # lookup rather than by listing a node and guessing. Capability-detected
    # on purpose — support landed in Scrapyd 1.2 and some builds reject
    # unknown form fields. B3 Step 0's live check (see
    # ``app_shared.scrapyd.client._DETERMINISTIC_JOBID_SETTING``) proved the
    # deployed Scrapyd 1.6.0 honours it verbatim, so this now defaults ON
    # (EPA B3b): with it off, a run that has already started (left the
    # pending queue) is uncorrelatable and `reconcile_inflight` can only
    # answer AMBIGUOUS, not adopt it. A node that ignores the field simply
    # answers with its own jobid, which is recorded exactly as before.
    SCRAPYD_DETERMINISTIC_JOBID: bool = True
    # How many times one target may be handed back DEFERRED before it is
    # called a terminal FAILED (2026-08-03; consumed by the scraping
    # runtime's defer-budget helper). DEFERRED is non-terminal, so without
    # a bound a persistently blocked domain cycles forever and its job
    # never finalizes.
    SCRAPE_MAX_DEFER_CYCLES: int = 3

    # --- EPA C1 (F08): per-TARGET deadline + physical attempt budget ------
    # `SCRAPE_JOB_MAX_RUNTIME_SECONDS` (12h) and `SCRAPE_MAX_DEFER_CYCLES`
    # bound the JOB and the DEFER loop respectively. Neither bounds what
    # ONE target may spend on physical fetches, which is where the money
    # goes: a target that escalates DIRECT -> IMPERSONATE -> PROXY ->
    # BROWSER and then retries each of those pays for every one of them,
    # and the deep dive found targets doing exactly that for the whole
    # job window.
    #
    # Wall-clock seconds one TARGET may stay non-terminal before it is
    # finalized `FAILED`/`TARGET_DEADLINE_EXCEEDED` WITHOUT another
    # fetch. Deliberately far below `SCRAPE_JOB_MAX_RUNTIME_SECONDS`:
    # one slow target may not hold a whole refresh open, and 15 minutes
    # is already ~20x the p95 target lifetime A5 measures.
    SCRAPE_TARGET_DEADLINE_SECONDS: int = 900
    # Physical fetches (one HTTP request or one browser navigation each;
    # a retry counts again) one target may spend in ONE refresh across
    # every method on its ladder. The 5th is refused
    # `ATTEMPT_BUDGET_EXHAUSTED`. 4 is the full ladder once with one
    # retry -- past that the evidence says the target is not gettable
    # today, and paying for a 5th attempt buys nothing.
    SCRAPE_TARGET_MAX_PHYSICAL_ATTEMPTS: int = 4
    # Fraction of targets whose cheap method has been suppressed by a
    # DOMAIN-level rule that still get one sampled recovery probe with
    # it. Without this a domain-wide suppression is permanent by
    # construction: the cheap method is never tried again, so it can
    # never produce the success that would lift the suppression. 5% is a
    # deliberate, bounded cost paid to keep the suppression falsifiable.
    # Per-TARGET suppression (this target, this refresh) is never probed
    # -- it is a fact about this fetch, not a standing rule.
    SCRAPE_RECOVERY_PROBE_FRACTION: float = 0.05

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

    # --- Two consumer pools, one service (EPA B4, F09) -----------------
    #
    # `apps/workers/start.sh` launches exactly two `celery worker`
    # processes in the one container: `critical@%h` (`scrape_dispatch`,
    # `maintenance` — dispatch plus the sweeps that keep jobs/breaker/
    # outbox state machines from getting stuck) and `bulk@%h`
    # (`price_analysis`, `strategy_discovery`, `webhook_events` — traffic
    # that can tolerate more queueing latency). Each process is started
    # with an explicit `-c` flag from these two settings, which
    # supersedes `CELERY_WORKER_CONCURRENCY` above for both pools (that
    # setting only still matters if a process is ever started without
    # `start.sh`'s explicit `-c`, e.g. a one-off `celery worker` shell
    # invocation). Isolating the two pools means an overloaded
    # `price_analysis`/`strategy_discovery` backlog can never starve the
    # `maintenance` consumers the fleet's reapers/reconcilers depend on.
    # See `docs/ops/CAPACITY.md` for the RAM budget this implies.
    CELERY_CRITICAL_CONCURRENCY: int = 2
    CELERY_BULK_CONCURRENCY: int = 2

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
    # --- EPA B3 (2026-09-07), closing B2's owed wiring: how often the
    # scheduler claims the durable `dispatch_reconcile` cadence, which
    # enqueues `DISPATCH_RECONCILE_INTENTS` -- step 5 of B2's
    # commit-before-send protocol
    # (`app_shared.jobs.dispatch_intents.reconcile_inflight_intents`).
    # B2 shipped that function with no schedule at all, so a worker
    # killed between its POST and the node's answer left a `POSTED` row
    # nothing ever settled.
    #
    # DURABLE, not an in-process accumulator: the whole point of the
    # protocol is to survive the process dying, so the cadence that
    # settles its leftovers must survive a restart too -- an in-process
    # float would reset on exactly the event it exists to clean up after.
    # 300s matches the outbox reconcile beside it: this is a bounded
    # scan of at most `DISPATCH_RECONCILE_LIMIT` POSTED rows plus one
    # `listjobs` call per distinct node.
    DISPATCH_RECONCILE_INTERVAL_SECONDS: int = 300
    # Rows examined per reconcile pass. Bounded for the same reason
    # `OUTBOX_DRAIN_BATCH_LIMIT` is: one pass must not hold a worker (or
    # its 300s time limit) for an unbounded time; the next tick continues.
    DISPATCH_RECONCILE_LIMIT: int = 200
    # --- R11 (2026-09-09): what it takes before absence counts as proof.
    #
    # The reconciler used to move a `POSTED` intent to
    # `RECONCILED_MISSING` — the one state that authorizes a re-POST, and
    # therefore a second cost authorization — on the strength of a SINGLE
    # `listjobs.json` answer that did not mention it, at any age. A pass
    # that ran while the original `schedule.json` request was still
    # travelling to the node got exactly that answer, so the reconciler
    # authorized a re-run of work already on its way. These four knobs are
    # the evidence bar that transition now has to clear.
    #
    # MIN_AGE is the in-flight LEASE: below it the request may simply not
    # have arrived yet, so a denial says nothing and is not even recorded.
    # 120s is 4x `ScrapydDispatchClient`'s own 30s HTTP timeout, so it
    # covers the whole life of a request the poster has not yet given up
    # on, plus a retry, plus clock skew between the worker and this sweep.
    DISPATCH_RECONCILE_MIN_AGE_SECONDS: int = 120
    # How many independent denials, and how far apart, before absence is
    # corroborated. Two answers spanning 120s cannot both be the same
    # in-flight window, and at the 300s pass cadence this costs one extra
    # pass (~5 min) before a genuinely lost dispatch is recovered — the
    # price of never re-running work that was merely slow.
    DISPATCH_RECONCILE_ABSENCE_QUORUM: int = 2
    DISPATCH_RECONCILE_ABSENCE_WINDOW_SECONDS: int = 120
    # Beyond this age a node cannot testify about the intent at all: both
    # deployed nodes run `MemoryJobStorage` with `finished_to_keep = 100`
    # and lose the whole history on restart, so a run that finished long
    # ago is indistinguishable from one that never started. Such an
    # intent goes to `RECONCILED_AMBIGUOUS` and waits for an operator
    # rather than being re-POSTed on evidence the node does not have.
    # 24h: comfortably longer than any real batch, far shorter than the
    # interval over which a node is certain to have restarted.
    DISPATCH_RECONCILE_ABSENCE_HORIZON_SECONDS: int = 86_400
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

    # --- Per-response download bounds (READY-013-c response-bomb cap,
    # Principle IV — env-tunable, never a hardcoded literal in the Scrapy
    # settings modules). Scrapy applies DOWNLOAD_MAXSIZE in three places,
    # so one knob closes all three response-bomb shapes at once:
    #
    #   * a declared ``Content-Length`` above the cap  -> refused before
    #     a single body byte is fetched (``_ScrapyAgent``),
    #   * a ``Content-Length`` that *lies* low, or a chunked stream that
    #     never ends -> the connection is cancelled the moment the bytes
    #     actually received cross the cap (``_ResponseReader``),
    #   * a small gzip/br/deflate body that inflates without bound ->
    #     aborted mid-inflate (``HttpCompressionMiddleware`` /
    #     ``_DecompressionMaxSizeExceeded``), so the bomb is never
    #     materialized in memory.
    #
    # Scrapy's own default is 1 GiB, which is not a bound for a price
    # scraper: one hostile competitor page could exhaust the spider
    # process. 8 MiB is ~20x the largest real product page observed
    # (~400 KB) — heavy legitimate pages are never touched. The warn mark
    # only logs, giving a signal before the cap ever bites.
    SCRAPE_DOWNLOAD_MAXSIZE_BYTES: int = 8 * 1024 * 1024
    SCRAPE_DOWNLOAD_WARNSIZE_BYTES: int = 2 * 1024 * 1024
    # Wall-clock companion to the byte cap: a slow-loris body that stays
    # under the cap forever is still an unbounded download without it.
    SCRAPE_DOWNLOAD_TIMEOUT_SECONDS: int = 60

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
    # --- Optimizer hysteresis (EPA W5.5-L2 Item B,
    # `app_shared.strategy.hysteresis`). The promotion evaluator's bar for
    # OVERWRITING a domain profile's preferred method was the same three
    # samples that install the very first one, with no reference to the
    # incumbent's track record, how long the candidate had been observed,
    # or whether the two methods were simply trading the preference back
    # and forth. These five knobs are the minimum-evidence hold, the
    # anti-flap band and the degradation rollback. ---
    #
    # Master switch. `False` restores byte-for-byte the pre-W5.5-L2
    # optimizer behaviour (no hold, no band, no rollback).
    STRATEGY_SWITCH_HYSTERESIS_ENABLED: bool = True
    # Distinct qualifying URLs a candidate must have accumulated before it
    # may REPLACE an existing preferred method. 6 = double the 3 that
    # installs a first-ever method: the incumbent's own track record is
    # evidence, and three samples must not outweigh it. Below this the
    # decision is downgraded to "hold current" -- the evidence is not
    # discarded, it keeps accumulating (the distinct-URL SET survives
    # every drain until a method actually promotes).
    STRATEGY_SWITCH_MIN_EVIDENCE_SAMPLES: int = 6
    # Minimum wall-clock span that evidence must cover, so a burst of
    # successes inside one flush interval cannot repoint a domain. 1800s
    # (30 min) is 30 x the 60s flush cadence.
    STRATEGY_SWITCH_MIN_EVIDENCE_WINDOW_SECONDS: int = 1800
    # WIDER band for switching BACK to a method a previous switch already
    # rolled back: 2.0 x the sample requirement (6 -> 12). Asymmetry is
    # the whole anti-flap mechanism -- returning costs more than leaving
    # did, so alternating samples converge instead of oscillating.
    STRATEGY_SWITCH_REVERT_EVIDENCE_MULTIPLIER: float = 2.0
    # Attempts that must be recorded on the NEW method strictly after a
    # switch before its outcome is judged at all -- never revert on one
    # sample. Mirrors STRATEGY_METHOD_BREAKER_MIN_ATTEMPTS' 10.
    STRATEGY_SWITCH_ROLLBACK_MIN_ATTEMPTS: int = 10
    # How far BELOW the replaced method's success rate (measured at switch
    # time) the new method must fall before the switch is treated as a
    # degradation and reverted. A margin, not equality, so ordinary noise
    # never triggers a revert; 0.20 is the same order as the 0.80
    # rediscovery success-rate floor.
    STRATEGY_SWITCH_ROLLBACK_DEGRADATION_MARGIN: float = 0.20

    STRATEGY_METHOD_BREAKER_MIN_ATTEMPTS: int = 10
    STRATEGY_METHOD_BREAKER_FAILURE_RATE: float = 0.80
    STRATEGY_METHOD_BREAKER_COOLDOWN_SECONDS: int = 1800
    STRATEGY_METHOD_BREAKER_CANARY_INTERVAL_SECONDS: int = 600
    STRATEGY_DISCOVERY_MIN_SAMPLE: int = 3
    STRATEGY_DISCOVERY_MAX_SAMPLE: int = 10
    STRATEGY_STATS_FLUSH_INTERVAL_SECONDS: int = 60
    STRATEGY_STATS_KEY_TTL_SECONDS: int = 3600

    # --- Discovery fleet-wide chunked scan (EPA B4, F09) ---------------
    # `STRATEGY_DISCOVERY_SCAN` (`app.workers.tasks_strategy.
    # strategy_discovery_scan`) processes at most this many
    # `DISCOVERY_REQUIRED` profiles per invocation, persisting how far it
    # got in `strategy_discovery_state` and re-enqueueing itself while a
    # pass is still in progress -- "resumable long maintenance" so a task
    # time limit or a worker restart mid-scan can never lose its place.
    # 20 keeps one invocation's outbox-write work (bounded DB work, no
    # blocking fetch) comfortably inside that task's own time_limit.
    STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN: int = 20

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

    # --- `build_recent_signals` domain-scoped join fix (Task 3.3,
    # proxy-cost-reduction plan, default OFF). Under
    # `STRATEGY_PROFILE_SCOPE="domain"`, `build_recent_signals` still
    # joins `competitor_product_matches.url_pattern ==
    # profile.url_pattern` -- but under domain scope `profile.url_pattern`
    # holds the bare competitor domain (no path), while a match's stored
    # `url_pattern` is `derive_url_pattern`'s host+path grouping key, so
    # the two are never equal (0 of 4,588 rows measured 2026-08-16):
    # rediscovery conditions 3, 5, 6, 7, 8 are silently dead code for
    # every domain-scoped profile. When `True`, the join instead matches
    # a registrable-domain comparison (reusing `rediscovery._bare_host`,
    # the same www-stripping fix as commit `36fd624`) so both
    # `www.`-prefixed and bare match rows for the profile's domain are
    # included. `False` (default) preserves today's exact-equality join
    # byte-for-byte -- the rollback path if enabling this reveals a
    # rediscovery/discovery loop, per the same cooldown/rate-limit
    # backstops `STRATEGY_REDISCOVERY_MIN_INTERVAL_SECONDS` and
    # `STRATEGY_DISCOVERY_MAX_RUNS_PER_KEY_PER_DAY` already enforce.
    # Ignored (no effect) under `STRATEGY_PROFILE_SCOPE="url_pattern"`,
    # where the exact-equality join is already correct.
    STRATEGY_SIGNALS_DOMAIN_JOIN: bool = False

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
    # EPA B5 (canary-gated): domains whose PROXIED browser legs fetch the
    # document and nothing else — every sub-resource is aborted before its
    # body crosses the paid proxy
    # (`app_shared.profiles.browser_resource_policy.should_block`,
    # `PROXIED_BLOCKED_RESOURCE_TYPES`). Comma-separated hostnames; a
    # listed domain also covers its subdomains (`amazon.sa` covers
    # `www.amazon.sa`). DEFAULT IS EMPTY AND MUST STAY EMPTY until the
    # owner's canary says otherwise: with `()` every domain's runtime
    # behaviour is byte-for-byte what it was before B5, on both
    # transports. A DIRECT browser leg is never affected at all — those
    # bytes are the fleet's own egress and cost nothing per byte.
    # NoDecode for the same reason SCRAPYD_*_URLS uses it: the env value
    # is a plain comma-separated string, never JSON.
    BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS: Annotated[tuple[str, ...], NoDecode] = ()

    # --- Connection-time browser egress guard (READY F01, plan task A1,
    # Principle IV — env-tunable, never a hardcoded literal). Chromium
    # performs its own DNS and follows redirects internally, so the
    # `PLAYWRIGHT_ABORT_REQUEST` route hook only ever sees the FIRST
    # request of a chain; the browser egress guard (in the scraping-core
    # library, module `browser.egress_guard` -- deliberately NOT named in
    # full here: `tests/unit/test_import_boundaries.py` forbids that
    # package's name anywhere under `app_shared`, comments included, so the
    # reverse dependency edge cannot be reintroduced by a lazy import) is
    # the enforcement point that every connection (redirect hop,
    # sub-resource, worker, popup, WebSocket) must pass through, because
    # Chromium is launched with `--proxy-server=http://127.0.0.1:<port>`.
    #
    # ON by default and intended to stay on: with it off, the browser node
    # is back to route-hook-only coverage, which is the exact gap F01
    # exists to close. The switch exists so an operator can prove a
    # production incident is or is not the guard, not as a routine knob.
    BROWSER_EGRESS_GUARD_ENABLED: bool = True
    # Wall clock for the guard's own upstream dial (origin or proxy leg).
    # Bounds a black-holed destination without waiting out the OS SYN
    # retry ladder; the navigation timeout above still bounds the page.
    BROWSER_EGRESS_GUARD_CONNECT_TIMEOUT_SECONDS: float = 10.0
    # Service workers persist past the page that registered them and can
    # re-issue fetches with no route hook attached, so they are blocked at
    # context creation. `"allow"` exists only for a diagnostic run against
    # a site that genuinely will not render without one.
    BROWSER_SERVICE_WORKERS: Literal["block", "allow"] = "block"

    # --- Retention, rollups & partition maintenance tuning (SPEC-15,
    # data-model.md §6, Principle IV — env/DB-tunable, never a hardcoded
    # literal). Five per-table retention windows, three maintenance-task
    # cadence intervals, and the partition create-ahead lookahead. Reused
    # unchanged: SYSTEM_DATABASE_URL (-> AUTH_DATABASE_URL fallback) — no
    # new session knob added here. ---
    # 180, not 90 (EPA C9 / owner decision 11, 2026-09-07): the raw
    # observation is the evidence a pricing decision traces back to, and
    # a 90-day window put it BELOW the 180-day dispute horizon the policy
    # doc now records. Changed in place rather than re-declared in C9's
    # append block below because two contradictory declarations of one
    # knob is a landmine, not a merge strategy.
    RETENTION_PRICE_OBSERVATIONS_DAYS: int = 180
    RETENTION_REQUEST_ATTEMPTS_DAYS: int = 90
    RETENTION_PRICE_ALERT_EVENTS_DAYS: int = 365
    RETENTION_WEBHOOK_EVENTS_DAYS: int = 90
    RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS: int = 730
    PARTITION_CREATE_INTERVAL_SECONDS: int = 86400
    DAILY_ROLLUP_INTERVAL_SECONDS: int = 86400
    RETENTION_INTERVAL_SECONDS: int = 86400
    # --- Seeded-entitlement staleness refresh (EPA go-live prep,
    # 2026-08-26). How often the scheduler re-stamps `observed_at` on
    # `workspace_entitlements` rows the seeder owns
    # (`app_shared.costauth.entitlements.refresh_seeded_entitlements`).
    #
    # This is the ONE interval in this block that is not daily, and the
    # reason is arithmetic rather than taste: the gate denies on evidence
    # older than `DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS` (86400),
    # so a daily refresh would put the deadline and the cadence on the
    # same number and make "did the tick land before the evidence
    # expired?" a coin flip every single day. 21600 (6h) leaves three
    # whole missed ticks of margin before any workspace starts denying,
    # which is enough for a redeploy, a broker hiccup, and an operator's
    # night's sleep. It costs one bounded UPDATE over one row per
    # workspace, four times a day.
    ENTITLEMENT_REFRESH_INTERVAL_SECONDS: int = 21600
    # --- Fleet budget cap policy (EPA A4/B3, 2026-09-03). The monthly
    # money ceiling `maintenance.fleet_budget_rollforward` writes onto
    # `fleet_cost_budgets` for each PAID transport class, in DOLLARS
    # (converted once, by `app_shared.costauth.fleet_budget_policy.
    # usd_to_units` — micro-USD since H4/B1 — so these numbers never had
    # to be re-derived when the ledger's unit changed).
    #
    # `None` is not "no cap" — it means "carry the last cap you find
    # forward", which is what keeps a deploy that forgot these vars from
    # silently uncapping the fleet. Only a scope with NO configured cap
    # AND no earlier capped period is genuinely uncapped, and that is
    # logged at ERROR from two places: `assert_production_safe` at
    # startup, and the cadence's own report every 6h.
    #
    # Deliberately unset by default: a default here would be this
    # repository inventing a money ceiling for someone else's provider
    # account. `scripts/seed_fleet_budget_cap.py --propose` derives the
    # number from observed spend and prints its full derivation.
    FLEET_BUDGET_MONTHLY_CAP_USD_PROXY: float | None = None
    FLEET_BUDGET_MONTHLY_CAP_USD_BROWSER: float | None = None

    # --- Proxy billing unit (EPA A8, deep dive §8.3). `costauth.pricing`
    # used to divide by a bare `2**30` literal named `BYTES_PER_GIB` —
    # a real ambiguity, because the September 3 pricing note itself
    # confuses `$/GB` with `$/GiB` twice, and a provider contract that
    # actually bills decimal GB would silently under/over-price every
    # proxied byte by ~7.4% with no setting anywhere to point at. Named
    # here so the unit is a visible, overridable choice rather than an
    # implicit constant: default `1073741824` (2**30, one GiB — the
    # DataImpulse pool rate this repo has actually recorded) reproduces
    # today's numbers bit-for-bit; a provider whose contract says decimal
    # `GB` sets this to `1000000000` and every downstream price recomputes
    # from the same rate.
    PROXY_BILLING_UNIT_BYTES: int = 1_073_741_824

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

    # --- Durable rollup watermark + bounded backfill (EPA W5.5-L2 Item A,
    # `app_shared.maintenance.rollup_watermark`). The daily rollup used to
    # target "yesterday UTC" computed fresh from the wall clock, with no
    # record of which days had actually been aggregated -- so any day the
    # deployment was down was never anybody's target again and was lost
    # for good once retention dropped its `price_observations` partition
    # (the 2026-07-11 -> 2026-08-12 backlog `scripts/backfill_daily_
    # rollups.py` exists to repair). These three knobs govern the durable
    # cursor that replaces the wall clock. ---
    #
    # Master switch. `False` restores the exact pre-watermark behaviour
    # (roll up yesterday, once, no cursor read or write) without needing
    # to revert the migration. Default `True` -- the guard is the point.
    ROLLUP_WATERMARK_ENABLED: bool = True
    # Bounded batch: the MAXIMUM number of owed UTC days one catch-up run
    # will process. 7 covers a week-long outage in one run while keeping a
    # single invocation's work bounded regardless of how long the gap
    # actually is; a longer gap simply takes more runs, each of which makes
    # bounded, durable progress. `0` freezes catch-up entirely (an
    # operator escape hatch) without losing the cursor.
    ROLLUP_BACKFILL_MAX_DAYS: int = 7
    # How far behind "yesterday UTC" the cursor is born on its very first
    # use. 1 means the first run after the migration does exactly the one
    # day the current code already does -- turning the cursor on is not
    # itself a historical backfill (that is `recompute_window`'s job, run
    # deliberately). Raising this makes the first run walk further back.
    ROLLUP_WATERMARK_SEED_LAG_DAYS: int = 1

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

    # --- Scheduler two-plane limits + weighted fair queuing (EPA W4.2,
    # report §6; `app_shared.scheduling.fair_queue`). ---
    #
    # Master switch, DEFAULT ON since EPA B3 (F07, 2026-09-07). `False`
    # keeps the SPEC-13 due-rule pass
    # (`app.scheduler.refresh.run_refresh_pass`) exactly as it is; `True`
    # swaps in the fair pass on the SAME `SCHEDULER_POLL_INTERVAL_SECONDS`
    # cadence and the SAME `SCHEDULER_CLAIM_BATCH_LIMIT` ceiling -- no new
    # interval knob.
    #
    # Shipped dark through W4.2..B4 and deferred on 2026-09-04 with the
    # reasoning "one tenant, so weighted fair queuing has nothing to
    # arbitrate" (`docs/DEFERRED-ITEMS.md`). B3 closes that deferral,
    # because fairness was never the only thing this flag gated. The fair
    # pass is also the ONLY path that carries per-rule failure isolation
    # with a bounded-retry ledger and a dead-letter sink, the two-plane
    # fleet/domain concurrency caps that keep one merchant's WAF from
    # seeing the fleet's whole backlog at once, and (B3) the durable
    # occurrence claim in `refresh_rule_occurrences`. None of those is a
    # tenant-count question, so the flag's value stopped being one.
    # FLEET-WIDE SCHEDULING BEHAVIOUR CHANGE, made deliberately -- see
    # EPA B3's report.
    SCHEDULER_FAIR_QUEUE_ENABLED: bool = True
    # FLEET PLANE: simultaneous in-flight fetches ONE DOMAIN may receive
    # from the whole fleet, counting every tenant. This is the number a
    # merchant's WAF sees; the per-workspace cap it replaces multiplied it
    # by the number of workspaces monitoring that merchant.
    SCHEDULER_FAIR_QUEUE_DOMAIN_CONCURRENCY: int = 4
    # FLEET PLANE: simultaneous in-flight fetches across every domain.
    SCHEDULER_FAIR_QUEUE_FLEET_CONCURRENCY: int = 64
    # Consecutive unexpected failures one scheduler item may accumulate
    # before it is dead-lettered (rule disabled + `scheduler.item.
    # dead_lettered` webhook event) instead of aborting the pass.
    SCHEDULER_FAIR_QUEUE_MAX_ATTEMPTS: int = 3

    # --- Same-URL job-planning coalescing (EPA W4.3, report §8;
    # `app_shared.jobs.coalescing`). Single-workspace, Stage 1 only --
    # cross-workspace coalescing is a separate, owner-gated decision and
    # nothing here builds toward it (every caller resolves targets for
    # exactly one workspace at a time already).
    #
    # Master switch. Shipped OFF for the W4.3 canary period; flipped to
    # DEFAULT ON by EPA plan task B4 (2026-09-04, "H3") once the canary
    # ran clean -- duplicate URL fetches across matches are free to
    # remove, and there is no reason to keep paying for them by default.
    # `False` means `cluster_for_coalescing` is never called and
    # `plan_batches` receives targets in their original order --
    # byte-identical to pre-W4.3 planning; that OFF path stays reachable
    # (and pinned by `tests/unit/test_w4_flag_defaults.py`, which now
    # asserts the ON default and that `False` still overrides from the
    # environment) for an operator who needs to roll the behaviour back.
    # The byte-identity of unreordered planning itself is pinned by the
    # pre-existing, unmodified `tests/unit/test_jobs_batching.py`.
    # (Neither pin is `tests/unit/test_jobs_batching_coalescing.py`,
    # which this comment used to name -- that suite exercises the
    # coalescing helpers and never reads `Settings`, so it could not have
    # caught a flipped default. W4 gate review, 2026-08-26.) `True`
    # reorders the targets handed to `plan_batches` so matches sharing a
    # canonical URL (the SAME `canonical_url_hash` the network ledger
    # groups on) land in the same dispatch chunk instead of splitting
    # across one by accident of input order -- it never changes
    # `plan_batches` itself, a batch's match_id cardinality, or cost
    # authorization (still estimated off `len(batch.match_ids)`,
    # unchanged).
    #
    # This flag is independent of the pre-existing `SCRAPE_URL_DEDUP`
    # (2026-08-11 proxy-cost Fix 1, spider layer): that flag folds
    # same-run identical-URL fetches onto one fetcher ONLY within
    # whatever match_ids one spider run already received: this one makes
    # sure same-URL matches actually reach the SAME spider run in the
    # first place. Both must be enabled for a duplicate fetch to
    # physically collapse; `SCRAPE_URL_DEDUP` itself is untouched by
    # Task B4 and still defaults `False` -- flipping it is a separate,
    # not-yet-made decision.
    JOBS_COALESCING_ENABLED: bool = True
    # The freshness-window bound a FUTURE cache-reuse pass would gate on
    # (reusing a recent-enough COMPLETED fetch with no new fetch at all --
    # see `app_shared.jobs.coalescing` module docstring for why that half
    # of the plan is not wired into dispatch yet). Conservative default:
    # short enough that reusing a fetch this old is unlikely to serve a
    # stale price to a second match. Read only by
    # `coalescing.is_within_freshness_window` today (unit-tested and
    # exercised by the W4.3 canary script), not by any live dispatch path.
    JOBS_COALESCING_FRESHNESS_SECONDS: int = 300

    # --- Capacity-aware Scrapyd placement (EPA B6, F11) -----------------
    # The most batches `choose_node` will queue behind ONE node before it
    # calls that node saturated and looks elsewhere; when every node in
    # the pool is at this bound the batch is DEFERRED, not POSTed (see
    # `app_shared.jobs.nodes.choose_node`). It bounds `pending`, not
    # `running`: the browser nodes run `max_proc = 1`
    # (`apps/scrapers-browser/scrapyd.conf`), so anything beyond the one
    # running spider is queue depth that a POST cannot shorten -- it only
    # holds a cost-authorization grant and a `claimed_at` stamp open
    # while the phase clock runs. 4 is ~4 browser runs deep, a few
    # minutes of work at current run times, which is enough buffer to
    # absorb a burst without letting one job monopolise a node.
    SCRAPYD_MAX_PENDING_PER_NODE: int = 4

    # --- EPA B5 (F10): FLEET-wide host admission ---------------------------
    # Every limiter setting above this block is per-WORKSPACE. These three
    # are the opposite and that is the point: the host sees one fleet, so
    # ten tenants each politely inside their own ceiling still hit
    # `amazon.sa` with ten times that. `app_shared.limiter.fleet.admit_fleet`
    # takes one lease per physical request (a browser navigation and an
    # HTTP request are one lease each; a retry re-acquires) against these
    # defaults, overridable per domain by the `domain_rules` row's
    # `fleet_concurrency` / `fleet_rate_per_minute` (NULL = use the
    # default here).
    #
    # Simultaneous in-flight physical requests the WHOLE fleet may hold
    # against one (domain, transport). Deliberately small: this is a
    # politeness ceiling toward a third-party host, not a throughput
    # target -- exceeding it is what gets the fleet's whole IP range
    # blocked, and the queueing it causes is absorbed by the existing
    # backoff/requeue/DEFERRED path, not by failing work.
    FLEET_HOST_CONCURRENCY_DEFAULT: int = 6
    # Physical requests per minute the WHOLE fleet may make against one
    # (domain, transport) -- the shared token bucket's capacity.
    FLEET_HOST_RATE_PER_MINUTE_DEFAULT: int = 90
    # Crash-recovery window for a fleet lease. A holder that dies without
    # releasing frees its slot this many seconds later (the semaphore
    # Lua purges expired members on every acquire -- no reaper). It must
    # comfortably exceed the longest single physical request, browser
    # navigations included, or a live fetch's slot is reclaimed while it
    # is still on the wire and the fleet quietly over-admits.
    FLEET_LEASE_TTL_SECONDS: int = 120

    @field_validator("SCRAPYD_HTTP_URLS", "SCRAPYD_BROWSER_URLS", mode="before")
    @classmethod
    def _parse_url_pool(cls, value: object) -> object:
        if isinstance(value, str):
            return _split_pool(value)
        return value

    @field_validator("BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS", mode="before")
    @classmethod
    def _parse_document_only_domains(cls, value: object) -> object:
        """``"amazon.sa, noon.com"`` -> ``("amazon.sa", "noon.com")``.

        Same comma-separated convention as the Scrapyd URL pools (never
        JSON), lowercased so a Railway value's casing can never make a
        listed domain silently miss.
        """
        if isinstance(value, str):
            return tuple(item.strip().lower() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(item).strip().lower() for item in value if str(item).strip())
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

    @field_validator("SCRAPE_FLUSH_RETRY_BACKOFF_SECONDS", mode="before")
    @classmethod
    def _parse_retry_backoff(cls, value: object) -> object:
        """``"1,5,30"`` -> ``(1.0, 5.0, 30.0)``.

        Same comma-separated convention as the Scrapyd URL pools (never
        JSON). An empty value is rejected rather than silently meaning
        "retry immediately, forever": the tuple is a schedule, and a
        schedule with no entries is a misconfiguration.
        """
        if isinstance(value, str):
            parsed = tuple(float(item.strip()) for item in value.split(",") if item.strip())
        elif isinstance(value, (list, tuple)):
            parsed = tuple(float(item) for item in value)
        else:
            return value
        if not parsed:
            raise ValueError("SCRAPE_FLUSH_RETRY_BACKOFF_SECONDS must have at least one delay")
        if any(delay < 0 for delay in parsed):
            raise ValueError("SCRAPE_FLUSH_RETRY_BACKOFF_SECONDS delays must be >= 0")
        return parsed

    # --- BEGIN EPA C7 (F12) APPEND BLOCK -- set-based daily rollups ------
    #
    # Appended here rather than beside the other `ROLLUP_*` fields above
    # so this task could land alongside a concurrent edit to the same
    # file without either side rewriting the other's region. Pydantic
    # collects annotated class attributes regardless of where they sit
    # relative to the validators, so these are ordinary `Settings` fields.
    #
    #: Keyset window: how many `(workspace_id, product_variant_id)` pairs
    #: ONE rollup statement covers. Each batch is its own transaction and
    #: its own durable checkpoint, so this is the granularity at which a
    #: killed invocation loses work -- and the granularity at which it
    #: resumes. 5,000 amortises the per-statement planning cost while
    #: keeping one transaction short.
    ROLLUP_BATCH_SIZE: int = 5000
    #: Wall-clock budget for ONE `MAINTENANCE_DAILY_ROLLUP` invocation,
    #: checked BETWEEN batches. Deliberately below the task's 1,800 s
    #: Celery `time_limit` (`apps/workers/app/workers/celery_app.py`,
    #: `_ROLLUP_LIMITS`) so the task stops itself cleanly and re-enqueues
    #: instead of being SIGKILLed mid-transaction: a self-stop keeps the
    #: last batch's checkpoint, a SIGKILL rolls it back. The 300 s of
    #: slack is one batch's worth of headroom on a slow day.
    ROLLUP_INVOCATION_BUDGET_SECONDS: int = 1500
    # --- END EPA C7 (F12) APPEND BLOCK -----------------------------------

    # --- BEGIN EPA C9 (F14) APPEND BLOCK -- retention per data class -----
    #
    # Appended (not interleaved with the SPEC-15 retention block above)
    # for the same reason C7's block is: this file is edited by several
    # concurrent tasks and an append cannot conflict with theirs.
    #
    # The three windows the plan re-states -- `RETENTION_REQUEST_ATTEMPTS_DAYS`
    # (90), `RETENTION_PRICE_OBSERVATIONS_DAYS` (180) and
    # `RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS` (730) -- are NOT
    # re-declared here: they already exist above and the only one whose
    # value changed (price observations, 90 -> 180) was edited in place
    # so there is exactly one declaration of each knob.
    #
    #: Browser SUBRESOURCE operations (`network_operations` rows carrying a
    #: `parent_operation_id`). 30 days, deliberately the shortest window in
    #: this block: a subresource is a means, not evidence. What must survive
    #: is the PARENT's exact totals, and
    #: `app_shared.maintenance.ledger_summaries` preserves those by writing
    #: one `network_operation_resource_summaries` row before the children go
    #: -- summarised, never merely deleted.
    RETENTION_NETWORK_OPERATION_CHILDREN_DAYS: int = 30
    #: Parent (non-child) physical operations. 730 days because this is the
    #: fleet's own cost evidence: it is what a provider invoice is
    #: reconciled against, and a financial record's window is set by the
    #: longest applicable obligation, not by storage cost.
    RETENTION_NETWORK_OPERATIONS_DAYS: int = 730
    #: `network_operation_allocations` -- the tenant-visible half of the
    #: same fact. Deliberately EQUAL to `RETENTION_NETWORK_OPERATIONS_DAYS`:
    #: an allocation outliving its operation is an orphan, and an operation
    #: outliving its allocations is a cost nobody owns. Two knobs rather
    #: than one because a future jurisdiction may demand tenant-side
    #: deletion earlier than fleet-side; if they ever diverge the
    #: allocation window must be the SHORTER one.
    RETENTION_COST_ALLOCATIONS_DAYS: int = 730
    #: `scrape_job_targets` -- per-target execution state. 90 days: it is
    #: operational telemetry whose durable outcome already lives in
    #: `price_observations`/`match_current_prices`.
    RETENTION_SCRAPE_JOB_TARGETS_DAYS: int = 90
    #: `dispatch_intents` -- the commit-before-send protocol's in-flight
    #: record. 30 days: a TERMINAL intent older than the reconcile window
    #: (`DISPATCH_RECONCILE_INTERVAL_SECONDS`, 5 min) by four orders of
    #: magnitude answers no question anyone can still ask.
    RETENTION_DISPATCH_INTENTS_DAYS: int = 30
    #: `cost_reservations` -- one row per C3 authorization grant. 30 days
    #: past SETTLED/RELEASED. The money fact it produced is on
    #: `cost_budgets` and in the ledger; the reservation itself is a lease.
    RETENTION_COSTAUTH_RESERVATIONS_DAYS: int = 30
    #: **The owner-ratification switch, and the reason nothing above can
    #: delete anything today.** Empty means EVERY retention family is
    #: inert: `app_shared.maintenance.retention.run_retention` and
    #: `app_shared.maintenance.ledger_summaries` both consult
    #: `app_shared.maintenance.registry.retention_class_enabled` before any
    #: DROP, DELETE or summarisation, and report the family as skipped
    #: instead. A window above is therefore a PROPOSED default, not a live
    #: policy, until the owner signs the matching "Ratified by owner on
    #: ____" line in `docs/RETENTION_POLICY.md` and names the class here.
    #:
    #: Values are the `RetentionFamily.class_key` strings in
    #: `app_shared.maintenance.registry.RETENTION_FAMILIES` (e.g.
    #: `"price_observations"`); `"*"` enables every registered family at
    #: once and exists so a post-ratification deployment is one value
    #: rather than nine. An UNKNOWN class name raises at `Settings`
    #: construction: a typo'd class must fail the deploy, never silently
    #: leave a family disabled that the owner believes they enabled.
    #:
    #: Env form is the repo's comma-separated pool convention (`NoDecode` +
    #: the validator below), never JSON.
    RETENTION_ENABLED_CLASSES: Annotated[list[str], NoDecode] = []
    #: How many parent operations one `MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN`
    #: invocation summarises. Each parent is its own transaction, so this
    #: bounds the invocation, not the lock.
    LEDGER_SUMMARIZE_BATCH_SIZE: int = 500
    #: Cadence for that task. Daily, like every other retention-shaped job.
    LEDGER_SUMMARIZE_INTERVAL_SECONDS: int = 86400
    #: How many rows ONE bounded retention `DELETE` removes
    #: (`app_shared.maintenance.retention._delete_expired_rows`). Each
    #: batch is its own transaction, so this is the granularity at which a
    #: killed invocation stops -- and the size of the largest lock and WAL
    #: record it can produce.
    RETENTION_ROW_DELETE_BATCH_SIZE: int = 5000
    #: How many such batches ONE `MAINTENANCE_RETENTION_DROP` invocation
    #: runs per family before leaving the rest to the next tick. 200 x
    #: 5,000 = one million rows per family per day, which clears any
    #: realistic day's arrivals while keeping the task far inside its
    #: Celery time limit even on the first (backlog-clearing) run.
    RETENTION_ROW_DELETE_MAX_BATCHES: int = 200

    @field_validator("RETENTION_ENABLED_CLASSES", mode="before")
    @classmethod
    def _parse_retention_enabled_classes(cls, value: object) -> object:
        """``"price_observations,request_attempts"`` -> ``[...]``.

        Comma-separated, the same convention as the Scrapyd URL pools --
        never JSON. An empty/whitespace value is the DEFAULT posture
        (nothing enabled), not an error: "delete nothing" is always a
        valid configuration of a deletion switch.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return value

    @field_validator("RETENTION_ENABLED_CLASSES", mode="after")
    @classmethod
    def _validate_retention_enabled_classes(cls, value: list[str]) -> list[str]:
        """Reject a class name no registry family answers to.

        Imported lazily inside the validator: `app_shared.maintenance.
        registry` imports `Settings` from this module, so a module-level
        import here would be circular.
        """
        from app_shared.maintenance.registry import RETENTION_CLASS_KEYS

        unknown = [item for item in value if item != "*" and item not in RETENTION_CLASS_KEYS]
        if unknown:
            raise ValueError(
                "RETENTION_ENABLED_CLASSES names unknown retention classes "
                f"{unknown!r}; known classes are {sorted(RETENTION_CLASS_KEYS)!r} "
                "(or '*' for all)"
            )
        return value

    # --- END EPA C9 (F14) APPEND BLOCK -----------------------------------

    # --- BEGIN EPA D5 (deep dive §12 item 9) APPEND BLOCK -- daily scorecard
    #
    # Appended (not interleaved with any block above) for the same reason
    # every other EPA append block here is: this file is edited by
    # several concurrent tasks and an append cannot conflict with theirs.
    #
    #: Cadence for `MAINTENANCE_DAILY_SCORECARD`
    #: (`app_shared.maintenance.scorecard.run_daily_scorecard`). Daily,
    #: like every other retention/rollup-shaped job — the scorecard row
    #: it writes is itself a closed CALENDAR DAY, so running it more than
    #: once a day would not produce a more current row, only a repeated
    #: one (the write is an idempotent UPSERT on `date`).
    SCORECARD_INTERVAL_SECONDS: int = 86400

    # --- END EPA D5 APPEND BLOCK -------------------------------------------

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
