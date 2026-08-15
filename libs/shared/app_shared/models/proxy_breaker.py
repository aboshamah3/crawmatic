"""``proxy_circuit_breakers`` ORM model — the durable, non-Redis half of
the independent spend circuit breaker (audit 2026-08-15 risk **H3**).

## Why this table exists at all

Every existing cost brake in this codebase is a Redis counter
(``proxybudget:*``, ``ratelimit:*``, ``cooldown:*``). A Redis-backed
counter structurally cannot protect against Redis failure: the exact
incident that blinds the ledger also removes the ceiling. The audit's
H3 fix therefore requires a stop-loss whose **authoritative state does
not live in Redis**. Postgres is already a hard dependency of the scrape
path (targets, policies and profiles are all loaded from it before any
fetch), so a single durable row read alongside that existing load costs
nothing extra in round trips and is available in precisely the failure
mode Redis is not.

## Shape: global, no RLS

Deliberately **no** ``workspace_id`` and **no** RLS — the same shape as
``domain_playbooks``. This is a platform-wide financial kill switch over
an operator-owned proxy account, not tenant data: the money is spent
against one shared DataImpulse balance, so one tenant's runaway loop
spends *everyone's* budget and the brake must be visible to, and
trippable by, every workspace's scrape path. There is no tenant CRUD
surface for it. ``scope_key`` carries the granularity instead (``global``
today; a provider id or domain later) without a schema change.

## Concurrency contract

``evaluated_at`` is the evaluator lease. Any process may attempt an
evaluation, but only the one that wins an atomic
``UPDATE ... WHERE evaluated_at < now() - interval ... RETURNING id``
actually recomputes the aggregates — so N spiders do not all scan
``request_attempts`` at once, and no external scheduler has to be wired
up for the breaker to work. See ``app_shared.access.breaker``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import enum_column
from app_shared.models.base import Base, TZDateTime, TimestampMixin


class ProxyBreakerState(StrEnum):
    """Breaker position.

    ``CLOSED`` — normal, paid requests permitted.
    ``OPEN``   — tripped, NEW proxied requests denied. In-flight fetches
                 are untouched (the gate is consulted only when deciding
                 the *next* attempt), so tripping never corrupts a job.
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"


class ProxyBreakerTrip(StrEnum):
    """Which condition tripped the breaker (audit §7's named cost signals)."""

    #: Month-to-date proxied requests exceeded the absolute ceiling.
    MONTHLY_SPEND = "MONTHLY_SPEND"
    #: Trailing-1h rate, extrapolated to month end, blows the ceiling.
    VELOCITY_1H = "VELOCITY_1H"
    #: Trailing-24h rate, extrapolated to month end, blows the ceiling.
    VELOCITY_24H = "VELOCITY_24H"
    #: Proxied requests per DISTINCT url too high — the runaway-loop
    #: signature (the same URL re-fetched over and over).
    REQUESTS_PER_URL = "REQUESTS_PER_URL"
    #: Strategy discovery runs per domain per day too high — the
    #: 2026-08-12 hostname-normalisation rediscovery loop's signature.
    DISCOVERY_RUNS_PER_DOMAIN = "DISCOVERY_RUNS_PER_DOMAIN"
    #: Tripped by an operator, not by an evaluation.
    MANUAL = "MANUAL"


class ProxyCircuitBreaker(Base, TimestampMixin):
    """``proxy_circuit_breakers`` — one durable row per ``scope_key``.

    Global (no ``workspace_id``, no RLS) — see the module docstring.
    ``observed`` is the JSONB snapshot of the measurements that produced
    the current verdict, so an operator can see *why* it tripped without
    re-running the aggregates.
    """

    __tablename__ = "proxy_circuit_breakers"
    __table_args__ = (
        Index("uq_proxy_circuit_breakers_scope_key", "scope_key", unique=True),
    )

    #: Granularity of this breaker. ``"global"`` is the only value the
    #: current evaluator writes; the column exists so a per-provider or
    #: per-domain breaker needs no migration.
    scope_key: Mapped[str] = mapped_column(Text(), nullable=False, default="global")
    state: Mapped[ProxyBreakerState] = enum_column(
        ProxyBreakerState, nullable=False, default=ProxyBreakerState.CLOSED
    )
    #: Which condition tripped it (NULL while CLOSED).
    trip_reason: Mapped[ProxyBreakerTrip | None] = enum_column(
        ProxyBreakerTrip, nullable=True
    )
    #: Human-readable explanation of the trip, for the alert/runbook.
    detail: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Measurement snapshot behind the current verdict.
    observed: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    tripped_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    #: Evaluator lease (see module docstring). Epoch-ish default so the
    #: very first evaluation always wins the lease.
    evaluated_at: Mapped[datetime] = mapped_column(
        TZDateTime(),
        nullable=False,
        default=lambda: datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    #: Monotonic count of trips, for alerting on flapping.
    trip_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)


#: The single scope the current evaluator reads and writes.
GLOBAL_BREAKER_SCOPE = "global"

__all__ = [
    "GLOBAL_BREAKER_SCOPE",
    "ProxyBreakerState",
    "ProxyBreakerTrip",
    "ProxyCircuitBreaker",
]
