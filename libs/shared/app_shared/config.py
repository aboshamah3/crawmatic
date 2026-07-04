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
    SCRAPE_STALL_TIMEOUT_SECONDS: int = 900

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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Parsed once per process on first call; subsequent calls return the
    cached instance.
    """
    return Settings()
