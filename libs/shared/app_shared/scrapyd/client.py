"""Authenticated, idempotent Scrapyd ``schedule.json`` dispatch client.

Framework-agnostic (plain ``requests`` + ``redis``, no scrapy/twisted): it
POSTs ``schedule.json`` on a ``SCRAPYD_HTTP_URLS`` node with HTTP basic auth
(``SCRAPYD_USERNAME`` / ``SCRAPYD_PASSWORD``) and forwards the spider args
through unchanged, returning the Scrapyd ``jobid``.

Idempotency ordering (the important bit — see ``schedule``)
----------------------------------------------------------
A retried at-least-once Celery dispatch must never double-run a batch, yet a
dispatch that *failed* (401 / network error) must never leave a poisoned key
that suppresses a later legitimate retry. We reconcile both with a
claim/commit/release sequence keyed on a stable
``dispatched:{scrape_job_id}:{batch_index}`` string:

1. **Claim** the slot with Redis ``SET key <pending-sentinel> NX`` *before* the
   network call. If the claim fails, the key already exists:
   - if it holds a real jobid -> return it as a **no-op** (never re-schedule);
   - if it still holds the sentinel, a concurrent dispatch is in flight -> raise
     (do not double-POST).
2. **Commit**: only after Scrapyd returns ``status=ok`` do we overwrite the
   sentinel with the real ``jobid`` (the durable backstop a retry returns).
3. **Release**: on *any* failure before commit (401, network error, non-ok
   response) we ``DELETE`` the key, so the claim never outlives a failed attempt
   and a legitimate retry can proceed.

The sentinel is therefore only ever visible while a POST is genuinely in
flight; a crash mid-flight leaves at most a short-lived sentinel, never a
permanent jobid for a run that never started.
"""

from __future__ import annotations

from typing import Any, Protocol

import requests

from app_shared.config import Settings, get_settings
from app_shared.redis_client import get_redis_client

__all__ = [
    "ScrapydAuthError",
    "ScrapydDispatchClient",
    "ScrapydDispatchError",
    "dispatch_key",
]

# Marks a claimed-but-not-yet-scheduled slot in Redis. Distinct from any real
# Scrapyd jobid (which is a hex string) so we can tell "in flight" from "done".
_PENDING_SENTINEL = "__dispatch_pending__"

# Conservative default; a dispatch POST that hangs must not block a worker.
_DEFAULT_TIMEOUT_SECONDS = 30.0


class ScrapydDispatchError(RuntimeError):
    """A Scrapyd dispatch could not be completed."""


class ScrapydAuthError(ScrapydDispatchError):
    """Scrapyd rejected the credentials (HTTP 401) — no run was scheduled."""


class _RedisLike(Protocol):
    """The tiny Redis surface this client needs (``decode_responses=True``)."""

    def set(  # noqa: D102 - protocol stub
        self, name: str, value: str, *, nx: bool = ..., ex: int | None = ...
    ) -> bool | None: ...

    def get(self, name: str) -> str | None: ...  # noqa: D102 - protocol stub

    def delete(self, *names: str) -> int: ...  # noqa: D102 - protocol stub


def dispatch_key(scrape_job_id: str, batch_index: int | str) -> str:
    """Stable idempotency key for one (job, batch) dispatch."""
    return f"dispatched:{scrape_job_id}:{batch_index}"


class ScrapydDispatchClient:
    """Schedules ``generic_price_spider`` runs on an authenticated Scrapyd node."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        redis_client: _RedisLike | None = None,
        session: requests.Session | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._redis = redis_client if redis_client is not None else get_redis_client()
        self._session = session
        self._timeout = timeout

    def schedule(
        self,
        project: str,
        spider: str,
        *,
        workspace_id: str,
        scrape_job_id: str,
        match_ids: Any,
        mode: str,
        batch_index: int | str,
    ) -> str:
        """Schedule ``spider`` in ``project`` on Scrapyd; return the ``jobid``.

        Idempotent per ``(scrape_job_id, batch_index)`` — see the module
        docstring for the claim/commit/release ordering that guarantees a
        retried dispatch never double-runs a batch and a *failed* dispatch never
        poisons the key.
        """
        key = dispatch_key(scrape_job_id, batch_index)

        # --- claim -----------------------------------------------------------
        # SET NX before the network call: whoever wins the claim owns the POST.
        claimed = self._redis.set(key, _PENDING_SENTINEL, nx=True)
        if not claimed:
            existing = self._redis.get(key)
            if existing and existing != _PENDING_SENTINEL:
                # Already scheduled — return the persisted jobid, do NOT re-POST.
                return existing
            # Sentinel still present -> a concurrent dispatch is mid-flight.
            raise ScrapydDispatchError(
                f"dispatch already in progress for {key!r}; not double-scheduling"
            )

        # --- schedule --------------------------------------------------------
        try:
            jobid = self._post_schedule(
                project,
                spider,
                workspace_id=workspace_id,
                scrape_job_id=scrape_job_id,
                match_ids=match_ids,
                mode=mode,
            )
        except BaseException:
            # release: never leave a poisoned key behind a failed attempt.
            self._redis.delete(key)
            raise

        # --- commit ----------------------------------------------------------
        # Persist the real jobid as the durable backstop for future retries.
        self._redis.set(key, jobid)
        return jobid

    def _post_schedule(
        self,
        project: str,
        spider: str,
        *,
        workspace_id: str,
        scrape_job_id: str,
        match_ids: Any,
        mode: str,
    ) -> str:
        base = self._settings.SCRAPYD_HTTP_URLS[0].rstrip("/")
        url = f"{base}/schedule.json"
        auth = (self._settings.SCRAPYD_USERNAME, self._settings.SCRAPYD_PASSWORD)
        # Spider args forwarded UNCHANGED (US4 scenario 3).
        data = {
            "project": project,
            "spider": spider,
            "workspace_id": workspace_id,
            "scrape_job_id": scrape_job_id,
            "match_ids": match_ids,
            "mode": mode,
        }

        poster = self._session.post if self._session is not None else requests.post
        response = poster(url, data=data, auth=auth, timeout=self._timeout)

        if response.status_code == 401:
            raise ScrapydAuthError(
                "Scrapyd rejected the dispatch credentials (HTTP 401); "
                "no run was scheduled"
            )
        if response.status_code >= 400:
            raise ScrapydDispatchError(
                f"Scrapyd schedule.json returned HTTP {response.status_code}"
            )

        payload = response.json()
        if payload.get("status") != "ok" or not payload.get("jobid"):
            raise ScrapydDispatchError(
                f"Scrapyd schedule.json did not return an ok jobid: {payload!r}"
            )
        return str(payload["jobid"])
