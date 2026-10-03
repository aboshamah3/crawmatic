"""Scheduled provider-usage evidence: fetch clients (2026-09-29, plan E7).

Why this exists
---------------
Reconciliation (`app_shared.netledger.reconcile`) compares the fleet's own
ledger against what the PROVIDER says it billed. In production that second
half had never existed: `provider_usage_records` held zero rows, because the
only importer was `scripts/import_dataimpulse_usage.py`, fed a file a human
exports by hand. So `cost_rollup.reconciliation_missing` fired CRITICAL every
day, correctly.

The interface
-------------
A :class:`ProviderUsageClient` turns "give me what you billed between two
dates" into :class:`~app_shared.netledger.reconcile.ProviderUsageSource`
objects that `import_provider_usage` persists (content-addressed, so a
re-fetch of an unchanged day is a no-op). The daily
`maintenance.reconcile_provider_usage` cadence fetches from every configured
client, imports, then reconciles.

DataImpulse
-----------
Implements the provider's DOCUMENTED gateway endpoint, verified against its
official API documentation (DataImpulse User API, Postman collection
``7041120/2sAY4rGRZC``, "Get Plan Statistics with History")::

    GET https://gw.dataimpulse.com:777/api/stats_with_history
    Authorization: HTTP Basic <plan login>:<plan password>
    -> {"traffic_history": [{"group_date": "2024-09-26T00:00:00Z",
                             "inbound_traffic": 634954, "outgoing_traffic": 282895,
                             "total_traffic": 917849, "requests_count": 259,
                             "errors": 0}, ...],
        "status": "ok", ...}

The documentation gives no date-range parameters, so the full history is
read and filtered here. Each entry is one UTC day of TOTAL traffic in bytes
with no per-host breakdown, which is why reconciliation treats host-less
evidence as one window-wide group. Only COMPLETED days are imported: today's
partial total would otherwise be stored as if it were the day's bill.

Credentials come from the environment only (`DATAIMPULSE_USAGE_API_LOGIN`,
`DATAIMPULSE_USAGE_API_PASSWORD`); never logged, never in an exception.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol

from app_shared.models.provider_usage import ProviderUsageGranularity
from app_shared.netledger.reconcile import ProviderUsageRow, ProviderUsageSource

logger = logging.getLogger(__name__)

__all__ = [
    "DataImpulseUsageClient",
    "ProviderUsageClient",
    "ProviderUsageFetchError",
    "configured_usage_clients",
]

DATAIMPULSE_PROVIDER = "dataimpulse"
DATAIMPULSE_DEFAULT_BASE_URL = "https://gw.dataimpulse.com:777"
_HISTORY_PATH = "/api/stats_with_history"


class ProviderUsageFetchError(RuntimeError):
    """The provider did not give a usable answer. Never carries a credential."""


class ProviderUsageClient(Protocol):
    provider: str

    def fetch(
        self, *, since: date, until: date, now: datetime | None = None
    ) -> list[ProviderUsageSource]:
        """Completed UTC days in ``[since, until]`` as importable sources."""
        ...


def _requests_get(url: str, *, auth: Any, timeout: float) -> Any:
    import requests

    return requests.get(url, auth=auth, timeout=timeout)


class DataImpulseUsageClient:
    """`GET /api/stats_with_history` -> one DAILY, host-less source per day."""

    provider = DATAIMPULSE_PROVIDER

    def __init__(
        self,
        *,
        login: str,
        password: str,
        base_url: str = DATAIMPULSE_DEFAULT_BASE_URL,
        http_get: Callable[..., Any] = _requests_get,
        timeout: float = 30.0,
    ) -> None:
        self._auth = (login, password)
        self._url = base_url.rstrip("/") + _HISTORY_PATH
        self._get = http_get
        self._timeout = timeout

    def _history(self) -> list[dict[str, Any]]:
        try:
            response = self._get(self._url, auth=self._auth, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - re-raised without the credential
            raise ProviderUsageFetchError(
                f"{self._url} unreachable: {type(exc).__name__}"
            ) from None
        if response.status_code >= 400:
            raise ProviderUsageFetchError(f"{self._url} answered HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            raise ProviderUsageFetchError(f"{self._url} returned non-JSON") from None
        if not isinstance(payload, dict):
            raise ProviderUsageFetchError(f"{self._url} returned {type(payload).__name__}")
        if payload.get("status") not in (None, "ok"):
            raise ProviderUsageFetchError(
                f"{self._url} status={payload.get('status')!r} message={payload.get('message')!r}"
            )
        history = payload.get("traffic_history")
        if not isinstance(history, list):
            raise ProviderUsageFetchError(f"{self._url} carried no traffic_history list")
        return [item for item in history if isinstance(item, dict)]

    def fetch(
        self, *, since: date, until: date, now: datetime | None = None
    ) -> list[ProviderUsageSource]:
        today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
        sources: list[ProviderUsageSource] = []
        for item in self._history():
            try:
                day = datetime.fromisoformat(str(item["group_date"]).replace("Z", "+00:00"))
                total = int(item["total_traffic"])
                requests_count = int(item.get("requests_count") or 0)
            except (KeyError, TypeError, ValueError):
                logger.warning("provider_usage.dataimpulse_unparseable_entry keys=%s", sorted(item))
                continue
            day_date = day.astimezone(timezone.utc).date()
            if not (since <= day_date <= until) or day_date >= today:
                continue
            start = datetime.combine(day_date, time.min, tzinfo=timezone.utc)
            canonical = json.dumps(
                {"source": self._url, "entry": item}, sort_keys=True, separators=(",", ":")
            ).encode()
            sources.append(
                ProviderUsageSource(
                    provider=self.provider,
                    window_start=start,
                    window_end=start + timedelta(days=1),
                    rows=[
                        ProviderUsageRow(
                            occurred_at=start,
                            target_host=None,
                            bytes_up=_int_or_none(item.get("outgoing_traffic")),
                            bytes_down=_int_or_none(item.get("inbound_traffic")),
                            total_bytes=total,
                            request_count=requests_count,
                            raw=dict(item),
                        )
                    ],
                    source_ref=f"dataimpulse:stats_with_history:{day_date.isoformat()}",
                    raw_bytes=canonical,
                    granularity=ProviderUsageGranularity.DAILY,
                )
            )
        sources.sort(key=lambda s: s.window_start)
        return sources


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def configured_usage_clients(settings: Any) -> Sequence[ProviderUsageClient]:
    """Every provider whose usage credential is present in the environment."""
    clients: list[ProviderUsageClient] = []
    login = getattr(settings, "DATAIMPULSE_USAGE_API_LOGIN", None)
    password = getattr(settings, "DATAIMPULSE_USAGE_API_PASSWORD", None)
    if login and password:
        clients.append(
            DataImpulseUsageClient(
                login=login,
                password=password,
                base_url=getattr(settings, "DATAIMPULSE_USAGE_API_BASE_URL", None)
                or DATAIMPULSE_DEFAULT_BASE_URL,
            )
        )
    return clients
