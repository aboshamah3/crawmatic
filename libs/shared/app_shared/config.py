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

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_pool(value: str) -> list[str]:
    """Split a comma-separated URL pool, stripping whitespace/empties.

    Treated as a pool even when it contains a single URL (FR-018).
    """
    return [item.strip() for item in value.split(",") if item.strip()]


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

    # --- Auth DB role (optional — direct BYPASSRLS role for pre-auth
    # credential lookups only; see app_shared.database.get_auth_session).
    # Deliberately never falls back to DATABASE_URL (SPEC-03 [analyze C1]).
    AUTH_DATABASE_URL: str | None = None

    # --- Argon2id tuning (optional — argon2-cffi defaults apply when unset) ---
    ARGON2_TIME_COST: int | None = None
    ARGON2_MEMORY_COST: int | None = None
    ARGON2_PARALLELISM: int | None = None

    @field_validator("SCRAPYD_HTTP_URLS", "SCRAPYD_BROWSER_URLS", mode="before")
    @classmethod
    def _parse_url_pool(cls, value: object) -> object:
        if isinstance(value, str):
            return _split_pool(value)
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Parsed once per process on first call; subsequent calls return the
    cached instance.
    """
    return Settings()
