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
