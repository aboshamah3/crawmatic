"""W4's two new feature flags default OFF (EPA W4 gate-review follow-up
F4, 2026-08-26).

Both W4 flags ship dark: the code paths they guard are merged, tested,
and reachable, but no deployment turns them on until a canary says so.
Nothing pinned that. ``config.py``'s own comment claimed the default was
"pinned by ``tests/unit/test_jobs_batching_coalescing.py``", which pins
planning *byte-identity* with the feature not applied -- it never reads
``Settings`` at all, so a one-character edit flipping either default to
``True`` would have shipped a fleet-wide behaviour change with a green
suite. This module is the missing pin, and it asserts the defaults
themselves rather than any downstream effect of them.

``_env_file=None`` is passed to every ``Settings(...)`` call, and only
the required variables are set, so these assertions see neither a
developer's local ``.env`` nor a real deployment's environment --
exactly the isolation convention ``tests/unit/test_config.py``
established.
"""

from __future__ import annotations

import pytest

from app_shared.config import Settings

# Same minimum required set `tests/unit/test_config.py` uses -- these are
# placeholders for a `Settings` that never connects to anything.
REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}

#: Every W4 flag that must ship dark, with the flag's own module.
W4_DARK_FLAGS = (
    ("SCHEDULER_FAIR_QUEUE_ENABLED", "app_shared.scheduling.fair_queue"),
    ("JOBS_COALESCING_ENABLED", "app_shared.jobs.coalescing"),
)


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # Belt and braces: even if the ambient environment sets one of the
    # flags, this module asserts the DEFAULT, so unset them explicitly.
    for flag, _module in W4_DARK_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    return Settings(_env_file=None)


def test_scheduler_fair_queue_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """W4.4's fair-queue scheduler ships dark: OFF means the existing
    scheduling path is untouched."""
    assert _settings(monkeypatch).SCHEDULER_FAIR_QUEUE_ENABLED is False


def test_jobs_coalescing_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """W4.3's same-URL coalescing ships dark: OFF means
    ``cluster_for_coalescing`` is never called and planning is
    byte-identical to pre-W4.3."""
    assert _settings(monkeypatch).JOBS_COALESCING_ENABLED is False


@pytest.mark.parametrize("flag,module", W4_DARK_FLAGS)
def test_w4_flag_default_is_the_boolean_false_not_merely_falsy(
    flag: str, module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is False``, not ``not settings.X``: a default of ``0``/``""``
    would satisfy a truthiness check while typing as something other
    than the ``bool`` the guarded call sites branch on."""
    value = getattr(_settings(monkeypatch), flag)
    assert value is False, f"{flag} (guarding {module}) must default OFF"


@pytest.mark.parametrize("flag,module", W4_DARK_FLAGS)
def test_w4_flag_can_still_be_enabled_from_the_environment(
    flag: str, module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dark by default, not unreachable -- the canary has to be able to
    turn it on, or "ships dark" would just mean "dead code"."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(flag, "true")
    assert getattr(Settings(_env_file=None), flag) is True
