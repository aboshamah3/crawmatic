"""Container-level memory self-watchdog (last line of defence, 2026-08-03).

On 2026-08-03 the `scrapers` service climbed to a 7.65 GB plateau and
held it for ~10 hours **while idle** (no running spider), until a manual
Railway restart. Nothing in-process noticed: Scrapy's own `MemoryUsage`
extension only guards a *spider* process, and Celery's
`worker_max_memory_per_child` only guards a *forked child* — a leak in
a long-lived parent (Scrapyd's twistd daemon, the Celery main process)
is invisible to both, and Railway has no memory-based restart policy.

This module closes that gap: a daemon thread reads the container's own
cgroup memory accounting every ``WATCHDOG_CHECK_INTERVAL_SECONDS`` and,
after ``WATCHDOG_CONSECUTIVE_BREACHES`` consecutive readings above
``WATCHDOG_MEMORY_LIMIT_MB``, logs CRITICAL and calls ``os._exit(1)``.
Railway then restarts the container, so any leak self-terminates within
minutes instead of holding gigabytes overnight.

Design notes:

* **Env-only configuration, never `Settings`.** The limit is a
  per-service operations knob set from the Railway dashboard (worker
  2048, scrapers 3072, scrapers-browser 3072), and this module is
  imported at process start in supervisor processes whose environment is
  not guaranteed to carry every required `Settings` field. A
  `ValidationError` here would take down the very process it protects,
  so the watchdog reads `os.environ` directly and *never* raises.
* **Unset or 0 disables it** — no thread is started, so a container
  without the env var behaves exactly as before.
* **Consecutive breaches, not one.** A single reading can catch a
  legitimate peak of a heavy run (~3k products); requiring N in a row
  over N × interval seconds distinguishes a plateau from a spike.
* **`os._exit(1)`, not `sys.exit`/`SIGTERM`.** The leaking process is by
  definition unhealthy and may be wedged in Twisted/Celery shutdown; a
  raised `SystemExit` in a daemon thread would be swallowed entirely.
  `os._exit` is immediate and unconditional, which is the point.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

# cgroup v2 (Railway/Docker on modern kernels) first, cgroup v1 fallback.
_CGROUP_V2_USAGE_PATH = "/sys/fs/cgroup/memory.current"
_CGROUP_V1_USAGE_PATH = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

_DEFAULT_CHECK_INTERVAL_SECONDS = 30.0
_DEFAULT_CONSECUTIVE_BREACHES = 3

_BYTES_PER_MB = 1024 * 1024

_started = threading.Lock()
_thread: threading.Thread | None = None


def read_container_memory_mb() -> float | None:
    """Return this container's current memory usage in MB, or ``None``.

    ``None`` means the cgroup files are absent or unreadable (e.g. a
    non-containerised dev machine) — the caller treats that as "cannot
    judge" and never kills the process on it.
    """
    for path in (_CGROUP_V2_USAGE_PATH, _CGROUP_V1_USAGE_PATH):
        try:
            with open(path, encoding="ascii") as handle:
                return int(handle.read().strip()) / _BYTES_PER_MB
        except (OSError, ValueError):
            continue
    return None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("memory watchdog: ignoring malformed %s=%r", name, raw)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _watch(limit_mb: float, interval_seconds: float, breaches_required: int) -> None:
    """Poll cgroup memory forever; exit the process on a sustained breach."""
    breaches = 0
    while True:
        usage_mb = read_container_memory_mb()
        if usage_mb is None:
            breaches = 0
        elif usage_mb > limit_mb:
            breaches += 1
            logger.warning(
                "memory watchdog: container at %.0f MB > limit %.0f MB "
                "(breach %d/%d)",
                usage_mb,
                limit_mb,
                breaches,
                breaches_required,
            )
            if breaches >= breaches_required:
                logger.critical(
                    "memory watchdog: container held %.0f MB > limit %.0f MB for "
                    "%d consecutive checks (%.0fs) — exiting so the platform "
                    "restarts this container",
                    usage_mb,
                    limit_mb,
                    breaches,
                    breaches * interval_seconds,
                )
                logging.shutdown()
                os._exit(1)
        else:
            breaches = 0
        threading.Event().wait(interval_seconds)


def start_memory_watchdog(service: str) -> bool:
    """Start the watchdog thread for ``service``; return whether it started.

    Idempotent and safe to call from any process-start hook: a second
    call is a no-op. Returns ``False`` when ``WATCHDOG_MEMORY_LIMIT_MB``
    is unset/0 (disabled) or the cgroup accounting is unreadable.
    """
    global _thread

    limit_mb = _env_float("WATCHDOG_MEMORY_LIMIT_MB", 0.0)
    if limit_mb <= 0:
        logger.info(
            "memory watchdog disabled for %s (WATCHDOG_MEMORY_LIMIT_MB unset/0)",
            service,
        )
        return False

    with _started:
        if _thread is not None and _thread.is_alive():
            return True

        usage_mb = read_container_memory_mb()
        if usage_mb is None:
            logger.warning(
                "memory watchdog not started for %s: no readable cgroup memory "
                "accounting (%s / %s)",
                service,
                _CGROUP_V2_USAGE_PATH,
                _CGROUP_V1_USAGE_PATH,
            )
            return False

        interval_seconds = _env_float(
            "WATCHDOG_CHECK_INTERVAL_SECONDS", _DEFAULT_CHECK_INTERVAL_SECONDS
        )
        breaches_required = _env_int(
            "WATCHDOG_CONSECUTIVE_BREACHES", _DEFAULT_CONSECUTIVE_BREACHES
        )
        _thread = threading.Thread(
            target=_watch,
            args=(limit_mb, interval_seconds, breaches_required),
            name=f"memory-watchdog[{service}]",
            daemon=True,
        )
        _thread.start()

    logger.info(
        "memory watchdog started for %s: limit %.0f MB, every %.0fs, "
        "%d consecutive breaches to exit (currently %.0f MB)",
        service,
        limit_mb,
        interval_seconds,
        breaches_required,
        usage_mb,
    )
    return True
