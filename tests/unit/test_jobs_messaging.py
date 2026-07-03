"""Enqueue-by-name producer seam unit tests (SPEC-08 T017, FR-006, FR-007).

`app_shared.messaging.enqueue` — exercised against a fake/patched Celery
producer (no real Redis/broker). Per contracts/messaging.md:

1. `enqueue(name, queue=..., kwargs=...)` routes to the right queue with
   the right task name + kwargs via `send_task`.
2. The producer is lazily constructed on first use (not at import time,
   not per-call — a per-process singleton), mirroring the
   `app_shared.redis_client`/`app_shared.database` lazy-singleton
   pattern this module's docstring cites.
"""

from __future__ import annotations

from typing import Any

import pytest

import app_shared.messaging as messaging_module
from app_shared.messaging import enqueue
from app_shared.task_names import SCRAPE_DISPATCH_JOB, SCRAPE_FINALIZE_JOBS


class _FakeCeleryProducer:
    """Stand-in for `celery.Celery` recording every `send_task` call."""

    def __init__(self, *, broker: str, backend: Any = None) -> None:
        self.broker = broker
        self.backend = backend
        self.calls: list[dict[str, Any]] = []

    def send_task(self, name: str, *, kwargs: dict[str, Any] | None, queue: str) -> None:
        self.calls.append({"name": name, "kwargs": kwargs, "queue": queue})


@pytest.fixture(autouse=True)
def _reset_producer_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with no cached producer and a patched Celery."""
    monkeypatch.setattr(messaging_module, "_producer", None)
    monkeypatch.setattr(messaging_module, "Celery", _FakeCeleryProducer)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://a:b@localhost:5432/c")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SCRAPYD_HTTP_URLS", "http://scrapers:6800")
    monkeypatch.setenv("SCRAPYD_BROWSER_URLS", "http://scrapers-browser:6800")
    monkeypatch.setenv("SCRAPYD_USERNAME", "scrapyd")
    monkeypatch.setenv("SCRAPYD_PASSWORD", "pw")
    monkeypatch.setenv("JWT_SECRET", "a" * 32)
    # Clear the cached pydantic-settings singleton so the monkeypatched env
    # vars above are actually seen by `get_settings()`.
    messaging_module.get_settings.cache_clear()
    yield
    messaging_module.get_settings.cache_clear()


def test_enqueue_routes_task_name_queue_and_kwargs() -> None:
    enqueue(SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={"scrape_job_id": "abc"})

    producer = messaging_module._producer
    assert isinstance(producer, _FakeCeleryProducer)
    assert len(producer.calls) == 1
    call = producer.calls[0]
    assert call["name"] == SCRAPE_DISPATCH_JOB
    assert call["queue"] == "scrape_dispatch"
    assert call["kwargs"] == {"scrape_job_id": "abc"}


def test_enqueue_supports_none_kwargs() -> None:
    enqueue(SCRAPE_FINALIZE_JOBS, queue="maintenance")

    producer = messaging_module._producer
    assert producer.calls[0]["kwargs"] is None
    assert producer.calls[0]["queue"] == "maintenance"


def test_producer_is_lazily_constructed_once_per_process() -> None:
    assert messaging_module._producer is None

    enqueue(SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={"a": 1})
    first_producer = messaging_module._producer
    assert first_producer is not None

    enqueue(SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={"a": 2})
    second_producer = messaging_module._producer

    # Same cached instance across calls — not rebuilt per enqueue.
    assert first_producer is second_producer
    assert len(first_producer.calls) == 2


def test_producer_bound_to_redis_url_from_settings() -> None:
    enqueue(SCRAPE_DISPATCH_JOB, queue="scrape_dispatch", kwargs={})

    producer = messaging_module._producer
    assert producer.broker == "redis://localhost:6379/0"
