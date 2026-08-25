"""Scrapyd dispatch error vocabulary (EPA B1).

Split out of :mod:`app_shared.scrapyd.client` so the *pure* identity
layer (:mod:`app_shared.scrapyd.identity`) and the durable intent store
(:mod:`app_shared.jobs.dispatch_intents`, a SQLAlchemy module) can raise
and catch the same exception types without importing ``requests`` — the
client module's HTTP dependency. ``client.py`` re-exports all three
names, so ``from app_shared.scrapyd.client import ScrapydDispatchError``
keeps working exactly as before.
"""

from __future__ import annotations

__all__ = [
    "ScrapydAuthError",
    "ScrapydDispatchError",
    "StaleCancellationGenerationError",
]


class ScrapydDispatchError(RuntimeError):
    """A Scrapyd dispatch could not be completed."""


class ScrapydAuthError(ScrapydDispatchError):
    """Scrapyd rejected the credentials (HTTP 401) — no run was scheduled."""


class StaleCancellationGenerationError(ScrapydDispatchError):
    """The work was authorized before a cancellation — refuse to POST it.

    A2's fence (``scrape_jobs.cancellation_generation``) is bumped inside
    the cancellation transaction, *before* anything outside Postgres is
    touched. A dispatch intent records the generation it was planned
    under (``cancellation_generation_at_creation``); if the job has moved
    on by the time the POST is attempted, that intent describes work a
    human has since closed. Raising here — rather than POSTing and
    letting the persistence-side fence discard the results — is the
    difference between spending nothing and spending a full scrape run
    on a cancelled job.
    """
