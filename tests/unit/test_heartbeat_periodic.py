"""`app_shared.heartbeat.PeriodicHeartbeat` (EPA B9, F22).

Every process class ("every 30s") needs the same thing: take a
`HeartbeatEmitter`, beat it on a background thread until told to stop.
This is a pure-timing unit test — a tiny `interval_seconds` keeps it fast
without needing a real clock or Redis.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app_shared.heartbeat import HEARTBEAT_EMIT_INTERVAL_SECONDS, HeartbeatEmitter, PeriodicHeartbeat


class _FakeRedis:
    """Records every `beat()` write instead of touching real Redis."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.beat_count = 0
        self._lock = threading.Lock()

    def incr(self, key: str) -> int:
        with self._lock:
            nxt = int(self.store.get(key, "0")) + 1
            self.store[key] = str(nxt)
            return nxt

    def set(self, key: str, value: str, ex: int | None = None, **_kw: Any) -> bool:
        with self._lock:
            self.store[key] = value
            if "heartbeat:" in key and "fence" not in key:
                self.beat_count += 1
        return True

    def get(self, key: str) -> str | None:
        return self.store.get(key)


def test_default_interval_is_30_seconds() -> None:
    assert HEARTBEAT_EMIT_INTERVAL_SECONDS == 30


def test_start_beats_immediately_and_then_periodically() -> None:
    redis_client = _FakeRedis()
    emitter = HeartbeatEmitter(redis_client, service="worker", instance_id="critical@test-host")
    periodic = PeriodicHeartbeat(emitter, interval_seconds=0.05)

    try:
        periodic.start()
        # The first beat happens synchronously inside `start()`'s loop
        # before the first wait — give the thread a brief moment to run it.
        deadline = time.monotonic() + 1.0
        while redis_client.beat_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert redis_client.beat_count >= 1

        # Let a couple more intervals pass.
        time.sleep(0.2)
        assert redis_client.beat_count >= 2
    finally:
        periodic.stop(timeout=1.0)


def test_stop_halts_further_beats() -> None:
    redis_client = _FakeRedis()
    emitter = HeartbeatEmitter(redis_client, service="worker", instance_id="bulk@test-host")
    periodic = PeriodicHeartbeat(emitter, interval_seconds=0.05)

    periodic.start()
    time.sleep(0.1)
    periodic.stop(timeout=1.0)
    count_at_stop = redis_client.beat_count

    time.sleep(0.2)

    assert redis_client.beat_count == count_at_stop


def test_start_is_idempotent() -> None:
    redis_client = _FakeRedis()
    emitter = HeartbeatEmitter(redis_client, service="scraper", instance_id="node-1")
    periodic = PeriodicHeartbeat(emitter, interval_seconds=0.05)

    try:
        periodic.start()
        thread_first = periodic._thread
        periodic.start()
        assert periodic._thread is thread_first
    finally:
        periodic.stop(timeout=1.0)


def test_thread_is_a_daemon_named_for_its_service_and_instance() -> None:
    redis_client = _FakeRedis()
    emitter = HeartbeatEmitter(redis_client, service="scraper", instance_id="node-7")
    periodic = PeriodicHeartbeat(emitter, interval_seconds=5.0)

    try:
        periodic.start()
        assert periodic._thread is not None
        assert periodic._thread.daemon is True
        assert periodic._thread.name == "heartbeat:scraper:node-7"
    finally:
        periodic.stop(timeout=1.0)
