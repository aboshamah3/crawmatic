"""W4's two feature flags' shipped defaults (EPA W4 gate-review follow-up
F4, 2026-08-26; flipped for `JOBS_COALESCING_ENABLED` by EPA plan task B4,
2026-09-04, "H3").

Both W4 flags shipped dark at first: the code paths they guard were
merged, tested, and reachable, but no deployment turned them on until a
canary said so. Nothing pinned that. ``config.py``'s own comment claimed
the original default was "pinned by
``tests/unit/test_jobs_batching_coalescing.py``", which pins planning
*byte-identity* with the feature not applied -- it never reads
``Settings`` at all, so a one-character edit flipping either default
would have shipped a fleet-wide behaviour change with a green suite.
This module is the missing pin, and it asserts the defaults themselves
rather than any downstream effect of them.

Task B4 turned ``JOBS_COALESCING_ENABLED`` on by default: duplicate URL
fetches across matches are free to remove now that the canary period
(W4.3) is over, and there is exactly one tenant in production today, so
there is no cross-workspace fan-in risk to gate behind a flag anymore.
``SCHEDULER_FAIR_QUEUE_ENABLED`` stays OFF -- it is a single-tenant
fleet today, so weighted fair queuing across workspaces has nothing to
arbitrate; it is deferred to 3+ tenants (see
``docs/DEFERRED-ITEMS.md``).

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

#: The two W4 flags this module pins, each with its own module and its
#: shipped default -- no longer both `False` since Task B4.
W4_FLAG_DEFAULTS = (
    ("SCHEDULER_FAIR_QUEUE_ENABLED", "app_shared.scheduling.fair_queue", False),
    ("JOBS_COALESCING_ENABLED", "app_shared.jobs.coalescing", True),
)


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # Belt and braces: even if the ambient environment sets one of the
    # flags, this module asserts the DEFAULT, so unset them explicitly.
    for flag, _module, _default in W4_FLAG_DEFAULTS:
        monkeypatch.delenv(flag, raising=False)
    return Settings(_env_file=None)


def test_scheduler_fair_queue_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """W4.4's fair-queue scheduler still ships dark: OFF means the
    existing scheduling path is untouched. Single-tenant fleet has
    nothing to arbitrate fairly between yet."""
    assert _settings(monkeypatch).SCHEDULER_FAIR_QUEUE_ENABLED is False


def test_jobs_coalescing_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task B4 (H3): same-URL coalescing now ships lit by default --
    ``cluster_for_coalescing`` reorders targets sharing a canonical URL
    into the same dispatch chunk, so a duplicate fetch across matches is
    free to remove."""
    assert _settings(monkeypatch).JOBS_COALESCING_ENABLED is True


@pytest.mark.parametrize("flag,module,default", W4_FLAG_DEFAULTS)
def test_w4_flag_default_is_a_real_boolean_not_merely_truthy_or_falsy(
    flag: str, module: str, default: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is default``, not ``bool(settings.X) == default``: a default of
    ``0``/``""``/``1`` would satisfy a truthiness check while typing as
    something other than the ``bool`` the guarded call sites branch on."""
    value = getattr(_settings(monkeypatch), flag)
    assert value is default, f"{flag} (guarding {module}) must default to {default!r}"


@pytest.mark.parametrize("flag,module,default", W4_FLAG_DEFAULTS)
def test_w4_flag_can_still_be_flipped_from_the_environment(
    flag: str, module: str, default: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever a flag's shipped default, an operator must still be able
    to override it from the environment in either direction -- a default
    is not a hard-coded value."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    flipped = not default
    monkeypatch.setenv(flag, "true" if flipped else "false")
    assert getattr(Settings(_env_file=None), flag) is flipped
