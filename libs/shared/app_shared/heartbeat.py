"""Per-instance service heartbeats with fencing (READY-001 Task A5, Step 7).

Audit ref: `PRODUCTION_READINESS_IMPLEMENTATION_PLAN_2026-08-25.md` Task A5
Step 7: "Worker/scheduler heartbeats carry **instance identity and monotonic
timestamps with fencing** (Redis ``heartbeat:{service}:{instance_id}``, 120s
TTL) so a stale or duplicate process cannot keep the global service heartbeat
healthy; ``/ready`` aggregates per-instance freshness."

Why a single global flag is the bug, not the feature
----------------------------------------------------

The naive design is one key per service (``heartbeat:worker``) refreshed by
whichever process gets there first. It reports "worker is alive" in three
situations that are not the same thing at all:

1. Every worker instance is healthy (the only one that should read healthy).
2. Nine of ten instances are dead and one is limping — the fleet has lost 90%
   of its capacity and the probe is green.
3. A *zombie* — a process that was replaced but never actually exited (an
   orphaned container, a fork that outlived its parent, a pod that missed its
   SIGTERM) — is still writing. Nothing real is consuming the queue, and the
   probe is green.

Keying by ``{service}:{instance_id}`` fixes (1) and (2): freshness is counted
per instance, so `/ready` can require a floor and an operator can see which
instances went quiet. Case (3) needs fencing.

Fencing
-------

Every emitter takes a **fencing token** at start: ``INCR
heartbeat:fence:{service}``, a strictly increasing integer owned by Redis, so
a process that starts later always holds a strictly higher token than one
that started earlier. A write is refused when the stored record's fence is
*higher* than the writer's own. So when an instance restarts under the same
identity, the new process's first beat raises the stored fence and the old
zombie is permanently locked out of that key — it can never again refresh a
TTL that would make the fleet look healthier than it is.

Monotonic timestamps
--------------------

Each record carries ``time.monotonic_ns()``. Within a single fence — i.e.
within one process's lifetime — a beat is refused unless it is strictly newer
than the stored one, which rejects replayed or reordered writes. Monotonic
clocks from *different* processes are not comparable (they count from
arbitrary per-process origins), which is exactly why the comparison is scoped
to a fence and a raised fence resets the baseline.

A separate ``wall_clock_epoch`` (`time.time()`) is recorded for *reporting*
age to a human. It is not trusted for expiry: the 120-second Redis TTL is the
authoritative staleness mechanism, because it cannot be fooled by clock skew
or by a paused process. Wall-clock age is applied as an additional upper
bound, so a record that somehow outlives its own budget still reads stale.

Failure posture
---------------

Aggregation is **fail-closed**: an unreadable Redis yields
``ok=False``. "I cannot tell whether the fleet is alive" is not a ready
state. Errors are reported as the exception CLASS NAME only, never the
message — a Redis connection error carries the URL (password included) in the
first characters of its text, and `/ready` is unauthenticated. This is the
same discipline `apps/api/app/routers/ready.py` already documents.

Emitting is **fail-open** at the call site: `HeartbeatEmitter.beat()` returns
``False`` rather than raising, so a Redis blip never crashes a worker loop.
A refused or failed beat simply means the key ages out and the fleet is
reported degraded — which is the truthful outcome.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "HEARTBEAT_EMIT_INTERVAL_SECONDS",
    "HEARTBEAT_KEY_PREFIX",
    "HEARTBEAT_TTL_SECONDS",
    "REQUIRED_SERVICES_ENV",
    "HeartbeatEmitter",
    "InstanceFreshness",
    "PeriodicHeartbeat",
    "ServiceFreshness",
    "aggregate_fleet_freshness",
    "aggregate_service_freshness",
    "default_instance_id",
    "fence_key",
    "heartbeat_key",
    "required_heartbeat_services",
    "write_heartbeat",
]

HEARTBEAT_KEY_PREFIX = "heartbeat"

#: 120 seconds, per the plan. Comfortably more than two beats at the
#: recommended 30-45s cadence, so one missed beat (a GC pause, a slow Redis
#: round trip) does not flap readiness, while a genuinely dead process is
#: reported within roughly two probe intervals.
HEARTBEAT_TTL_SECONDS = 120

#: Comma-separated service names `/ready` must find a fresh instance for,
#: e.g. ``"worker,scheduler"``.
#:
#: Deliberately a plain environment variable rather than an
#: `app_shared.config.Settings` field: `Settings` is `@lru_cache`d and
#: validated at import time across every app member, so adding a field there
#: makes this a startup-blocking concern for services that do not care about
#: it, and Task A5's deploy step is explicitly "the deploy only wires
#: config". Unset (the state before that wiring lands) means "no services
#: declared", which `/ready` reports as a labelled absence — never as a
#: failure, and never as a silent pass.
REQUIRED_SERVICES_ENV = "READY_REQUIRED_HEARTBEAT_SERVICES"

#: Optional per-service floor, e.g. ``"worker=2,scheduler=1"``. Absent
#: services default to a floor of 1.
REQUIRED_MIN_INSTANCES_ENV = "READY_HEARTBEAT_MIN_INSTANCES"

#: EPA B9 (F22, audit §13 Operations): "every process class" beats every
#: 30s. Comfortably inside `HEARTBEAT_TTL_SECONDS` (120s), so at least
#: three beats land within one TTL window even if one is delayed or
#: dropped by a GC pause or a slow Redis round trip.
HEARTBEAT_EMIT_INTERVAL_SECONDS = 30


def heartbeat_key(service: str, instance_id: str) -> str:
    """``heartbeat:{service}:{instance_id}`` (the plan's key shape, exactly)."""
    return f"{HEARTBEAT_KEY_PREFIX}:{service}:{instance_id}"


def fence_key(service: str) -> str:
    """``heartbeat:fence:{service}`` — the Redis-owned monotonically
    increasing counter every emitter for `service` draws its token from."""
    return f"{HEARTBEAT_KEY_PREFIX}:fence:{service}"


def _instance_scan_pattern(service: str) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}:{service}:*"


def default_instance_id() -> str:
    """A stable-per-process identity.

    Prefers the platform's own instance identifier when one exists
    (Railway sets ``RAILWAY_REPLICA_ID``; Kubernetes sets ``HOSTNAME`` to the
    pod name), because an operator reading `/ready` needs an id they can
    match against the platform's own dashboard. Falls back to
    ``{hostname}-{pid}-{random}`` so two processes on one host never collide.
    """
    for env_name in ("HEARTBEAT_INSTANCE_ID", "RAILWAY_REPLICA_ID", "HOSTNAME"):
        value = os.environ.get(env_name)
        if value:
            return value
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def required_heartbeat_services() -> tuple[str, ...]:
    """Declared services, parsed from ``REQUIRED_SERVICES_ENV``.

    Empty tuple when unset or blank — "nothing declared", which is a
    reportable absence rather than a pass or a failure.
    """
    raw = os.environ.get(REQUIRED_SERVICES_ENV) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def required_min_instances() -> dict[str, int]:
    """Per-service instance floors from ``REQUIRED_MIN_INSTANCES_ENV``.

    A malformed entry is ignored rather than raising: a typo in an ops
    variable must not take an API instance out of rotation, and the default
    floor of 1 is still enforced by `aggregate_service_freshness`.
    """
    floors: dict[str, int] = {}
    for pair in (os.environ.get(REQUIRED_MIN_INSTANCES_ENV) or "").split(","):
        name, sep, value = pair.partition("=")
        if not sep:
            continue
        try:
            floors[name.strip()] = int(value.strip())
        except ValueError:
            continue
    return floors


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def write_heartbeat(
    client: Any,
    *,
    service: str,
    instance_id: str,
    fence: int,
    monotonic_ns: int,
    wall_clock_epoch: float,
    ttl_seconds: int = HEARTBEAT_TTL_SECONDS,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Write one heartbeat record, honouring fencing and monotonicity.

    Returns ``True`` when the record was stored, ``False`` when the write was
    refused (a higher fence already holds the key, or this beat is not newer
    than the stored one within the same fence) or when Redis was unreachable.

    Refusal is deliberately indistinguishable to the caller from a Redis
    error, because the caller's correct response is identical in both cases:
    keep working, do not raise, and let the key age out if this really is a
    fenced-out process.

    Read-then-write rather than a Lua compare-and-set: the *only* writer of
    ``heartbeat:{service}:{instance_id}`` is the one process holding that
    instance identity, so there is no multi-writer race to serialise. A
    fenced-out zombie racing its replacement is exactly the case the fence
    comparison catches, and it catches it in the direction that matters — a
    lost race leaves the HIGHER fence in place, which is the correct record.
    """
    try:
        existing = _read_record(client, heartbeat_key(service, instance_id))
    except Exception:  # noqa: BLE001 - Redis unreachable; a beat never raises
        # Indistinguishable to the caller from a fenced-out refusal, and
        # deliberately so: the correct response is the same either way — keep
        # working, and let the key age out. Writing blind here (skipping the
        # fence check because we could not read it) would be worse: it could
        # let a zombie overwrite its replacement's record during exactly the
        # Redis instability that makes zombies likely.
        return False

    if existing is not None:
        stored_fence = existing.get("fence")
        if isinstance(stored_fence, int):
            if stored_fence > fence:
                return False  # fenced out by a newer process under this identity
            if stored_fence == fence:
                stored_monotonic = existing.get("monotonic_ns")
                if isinstance(stored_monotonic, int) and monotonic_ns <= stored_monotonic:
                    return False  # replayed / reordered beat within our own fence

    record: dict[str, Any] = {
        "service": service,
        "instance_id": instance_id,
        "fence": fence,
        "monotonic_ns": monotonic_ns,
        "wall_clock_epoch": wall_clock_epoch,
        "pid": os.getpid(),
        "ttl_seconds": ttl_seconds,
    }
    if extra:
        record.update(extra)

    try:
        client.set(
            heartbeat_key(service, instance_id),
            json.dumps(record, sort_keys=True, separators=(",", ":")),
            ex=ttl_seconds,
        )
    except Exception:  # noqa: BLE001 - a beat must never crash a worker loop
        return False
    return True


def _read_record(client: Any, key: str) -> dict[str, Any] | None:
    """Decode one heartbeat record, or ``None`` if absent/unreadable/corrupt.

    A corrupt record is treated as absent rather than as an error: it cannot
    prove freshness, and it must not be counted as a live instance.

    A Redis *error*, by contrast, propagates — "unreadable" and "absent" are
    different facts and each caller handles them differently:
    `aggregate_service_freshness` turns an error into a fail-closed
    `ok=False`, while `write_heartbeat` turns it into a refused beat.
    """
    raw = client.get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass
class HeartbeatEmitter:
    """One process's heartbeat writer for one service.

    Usage in a worker/scheduler loop::

        emitter = HeartbeatEmitter(get_redis_client(), service="worker").start()
        while True:
            emitter.beat()
            ...

    `start()` takes the fencing token and is separate from construction so
    the Redis round trip happens where a caller expects one, and so a
    constructed-but-not-started emitter is inert rather than half-live.
    """

    client: Any
    service: str
    instance_id: str = field(default_factory=default_instance_id)
    ttl_seconds: int = HEARTBEAT_TTL_SECONDS
    fence: int = 0

    def start(self) -> HeartbeatEmitter:
        """``INCR heartbeat:fence:{service}`` — take a token strictly higher
        than every emitter that started before us. Returns ``self`` so a
        caller can chain (``HeartbeatEmitter(...).start()``).

        A Redis failure here leaves ``fence`` at 0, which loses the zombie
        protection for this process but still lets it beat. That is the right
        trade: refusing to start a worker because a fencing counter was
        briefly unreachable would convert a monitoring outage into a
        processing outage.
        """
        try:
            self.fence = int(self.client.incr(fence_key(self.service)))
        except Exception:  # noqa: BLE001 - degrade to unfenced rather than refuse to run
            self.fence = 0
        return self

    def beat(self) -> bool:
        """Write one heartbeat. Never raises; see `write_heartbeat`."""
        return write_heartbeat(
            self.client,
            service=self.service,
            instance_id=self.instance_id,
            fence=self.fence,
            monotonic_ns=time.monotonic_ns(),
            wall_clock_epoch=time.time(),
            ttl_seconds=self.ttl_seconds,
        )


# --------------------------------------------------------------------------
# Aggregation (what `/ready` consumes)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceFreshness:
    instance_id: str
    fence: int | None
    age_seconds: float | None
    fresh: bool


@dataclass(frozen=True)
class ServiceFreshness:
    service: str
    instances: tuple[InstanceFreshness, ...]
    fresh_instance_count: int
    min_instances: int
    ok: bool
    #: Exception CLASS NAME when the aggregation itself failed, else ``None``.
    #: Never an exception message — see this module's docstring.
    error: str | None = None
    #: A short, operator-readable summary built entirely from names and
    #: counts this module produced. Never contains anything from an
    #: exception message, a Redis value, or a connection string.
    detail: str | None = None


def aggregate_service_freshness(
    client: Any,
    service: str,
    *,
    now_epoch: float | None = None,
    max_age_seconds: int = HEARTBEAT_TTL_SECONDS,
    min_instances: int = 1,
) -> ServiceFreshness:
    """Count how many instances of `service` are currently beating.

    Fail-closed: any Redis error yields ``ok=False`` with the exception class
    name. A record that is unreadable, corrupt, or older than
    `max_age_seconds` by its own wall clock is counted stale, not fresh.
    """
    now = time.time() if now_epoch is None else now_epoch
    instances: list[InstanceFreshness] = []

    try:
        for key in client.scan_iter(match=_instance_scan_pattern(service), count=100):
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="replace")
            instance_id = key.split(":", 2)[2] if key.count(":") >= 2 else key
            record = _read_record(client, key)
            if record is None:
                # Present-but-unreadable (or vanished between SCAN and GET).
                # Cannot prove liveness, so it is reported stale rather than
                # dropped — an operator should see that the key exists.
                instances.append(
                    InstanceFreshness(
                        instance_id=instance_id, fence=None, age_seconds=None, fresh=False
                    )
                )
                continue
            written_at = record.get("wall_clock_epoch")
            age = (
                now - float(written_at)
                if isinstance(written_at, (int, float))
                else None
            )
            fence_value = record.get("fence")
            fresh = age is not None and 0 <= age <= max_age_seconds
            instances.append(
                InstanceFreshness(
                    instance_id=instance_id,
                    fence=fence_value if isinstance(fence_value, int) else None,
                    age_seconds=age,
                    fresh=fresh,
                )
            )
    except Exception as exc:  # noqa: BLE001 - class name only, never the message
        return ServiceFreshness(
            service=service,
            instances=(),
            fresh_instance_count=0,
            min_instances=min_instances,
            ok=False,
            error=exc.__class__.__name__,
            detail=f"{service}: heartbeat store unreadable",
        )

    fresh_instances = tuple(item for item in instances if item.fresh)
    ok = len(fresh_instances) >= max(min_instances, 1)
    return ServiceFreshness(
        service=service,
        instances=fresh_instances,
        fresh_instance_count=len(fresh_instances),
        min_instances=min_instances,
        ok=ok,
        error=None,
        detail=(
            f"{service}: {len(fresh_instances)}/{max(min_instances, 1)} fresh instance(s)"
        ),
    )


def aggregate_fleet_freshness(
    client: Any,
    services: tuple[str, ...] | list[str],
    *,
    now_epoch: float | None = None,
    max_age_seconds: int = HEARTBEAT_TTL_SECONDS,
    min_instances: dict[str, int] | None = None,
) -> dict[str, ServiceFreshness]:
    """`aggregate_service_freshness` across every declared service."""
    floors = min_instances or {}
    return {
        service: aggregate_service_freshness(
            client,
            service,
            now_epoch=now_epoch,
            max_age_seconds=max_age_seconds,
            min_instances=floors.get(service, 1),
        )
        for service in services
    }


# --------------------------------------------------------------------------
# EPA B9 (F22): automatic periodic emission, one process class at a time
# --------------------------------------------------------------------------


@dataclass
class PeriodicHeartbeat:
    """Runs one `HeartbeatEmitter` on a background daemon thread, beating
    every `interval_seconds` (F22: "every 30s").

    A thin wrapper, not a scheduler of its own — every process class that
    needs a heartbeat constructs one of these at process start and calls
    `start()` once:

    * Scrapyd nodes: `apps/scrapers/price_monitor/scrapyd_app.py` and
      `apps/scrapers-browser/price_monitor_browser/scrapyd_app.py`, both
      `service="scraper"` (`heartbeat:scraper:<node>`).
    * Celery worker pools (`critical@`/`bulk@`, B4's `-n` node names):
      `apps/workers/app/workers/celery_app.py`, `service="worker"`,
      `instance_id` = the pool's own Celery hostname (`sender.hostname`
      on `worker_ready`, which carries the `-n critical@%h` /
      `-n bulk@%h` identity `apps/workers/start.sh` sets), stopped on
      `worker_shutdown`.
    * The scheduler: `apps/scheduler/app/scheduler/scheduler_app.py`'s
      `main()`, `service="scheduler"` — the one process class with no
      port for anything to poll.

    Together those three cover every long-lived process class, which is
    what makes `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker` a
    check that can actually pass (see `REQUIRED_SERVICES_ENV` above).

    `stop()` is best-effort, used by tests and graceful-shutdown hooks —
    never required for correctness: `HEARTBEAT_TTL_SECONDS` already ages
    out a beat that stops arriving.
    """

    emitter: HeartbeatEmitter
    interval_seconds: float = HEARTBEAT_EMIT_INTERVAL_SECONDS
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event | None = field(default=None, init=False, repr=False)

    def start(self) -> PeriodicHeartbeat:
        """Take the fencing token, beat once immediately, then every
        `interval_seconds` on a daemon thread. Idempotent — calling
        `start()` on an already-started instance is a no-op."""
        if self._thread is not None:
            return self
        self.emitter.start()
        stop_event = threading.Event()
        self._stop_event = stop_event

        def _loop() -> None:
            while True:
                self.emitter.beat()
                if stop_event.wait(self.interval_seconds):
                    return

        thread = threading.Thread(
            target=_loop,
            name=f"heartbeat:{self.emitter.service}:{self.emitter.instance_id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return self

    def stop(self, *, timeout: float = 1.0) -> None:
        """Signal the loop to exit and wait up to `timeout` for it. Safe to
        call on a never-started or already-stopped instance."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
