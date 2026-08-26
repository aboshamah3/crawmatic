"""Populate C1's physical ledger AT THE NETWORK BOUNDARY (EPA C4, READY-005).

C1 built three append-only tables and the triggers that keep them honest.
C3 built the gate that decides whether a paid dispatch may happen. Neither
of them writes a row when a socket actually opens — and a ledger nobody
populates is a schema, not an accounting system. This module is the
writer, and it sits at the ONE place where "a physical thing happened on
the wire" is a fact rather than an inference: immediately around the
transport call itself.

The two-call contract
---------------------
:meth:`NetLedgerRecorder.open` runs **before the socket opens** and
returns the ``network_request_id`` that identifies the operation
everywhere else. :meth:`NetLedgerRecorder.close` runs after the transport
is done and writes the outcome exactly once. Nothing else is allowed to
write these rows: a second writer is a second answer to "what did the
fleet spend".

Failure semantics — the two halves are deliberately NOT symmetric
-----------------------------------------------------------------
**Open failure fails the paid operation CLOSED.** If the ledger row
cannot be written, :meth:`open` raises :class:`LedgerOpenError` and the
caller must not dispatch. No socket without a ledger row: an
unrecordable fetch is money spent with no record of spending it, which is
strictly worse than a fetch that did not happen. This is the same
fail-closed posture C3 takes when it cannot evaluate a gate.

**Close failure NEVER fails the operation.** By the time close runs the
bytes have already moved; raising would neither un-spend them nor record
them. Instead the outcome is written to the host's durable local queue
(:mod:`app_shared.netledger.buffer`) as a ``CLOSE_RECOVERY`` event and
replayed by :meth:`NetLedgerRecorder.sweep`. The operation stays OPEN in
the ledger until that replay succeeds, which is exactly right: an open
row is a visible, queryable "we do not yet know what this cost", where a
dropped close would be an invisible zero.

Browser sub-resources are BUFFERED, not inserted
------------------------------------------------
One navigation pulls dozens of sub-resource fetches. Each is a real
physical operation and belongs in the ledger as a child row
(``parent_operation_id`` = the document operation) — but a synchronous
INSERT per asset would put a Postgres round-trip inside Chromium's own
response event loop. :meth:`buffer_child` appends to the durable local
queue instead; :meth:`flush` drains it in batches. Flush-on-close plus a
startup :meth:`sweep` of whatever the last process left behind means a
kill cannot lose events, and accounting never becomes the scraper's
per-asset bottleneck. This is what closes the canary's 68-recorded /
147-actual request-count gap: the 79 missing requests were sub-resources
nothing had a row for.

Which seam writes
-----------------
Every write here goes through the sanctioned **BYPASSRLS system seam**
(:func:`app_shared.database.get_system_session` / ``crawmatic_auth``),
per C1's ruling. The reason is the allocations table: its deferred total
trigger runs as the invoker and must aggregate EVERY workspace's share of
one operation, which FORCE RLS makes impossible on a tenant connection —
the trigger would see one row, compute a shortfall, and fail closed. The
operation row itself is fleet-owned (no ``workspace_id`` at all) and
tenant ownership lives only in the allocations, so a tenant connection has
no business writing either half.

Money
-----
Integer minor units throughout, per C1's columns; rates in
``billing_rate_micro_units`` scaled by
:data:`~app_shared.models.network_operations.BILLING_RATE_SCALE`. Splits
use :func:`~app_shared.models.network_operations.allocate_cost_largest_remainder`
so the parts sum EXACTLY — the deferred constraint trigger assumes that
rounding rule and rejects any other.

C3 wiring — accrue per operation, NEVER terminate the grant
------------------------------------------------------------
:meth:`heartbeat` renews the C3 lease while an operation is live (the
long browser navigations are why that method exists at all), and
:meth:`close` feeds the outcome to ``settle_partial()``.

``settle_partial``, deliberately, and it is the fix EPA Phase C's gate
review demanded. One C3 grant authorizes a whole BATCH — up to
``SCRAPE_DISPATCH_HTTP_BATCH_MAX`` targets, priced together — and this
boundary then opens one operation per target under it. Calling the
terminal ``settle()`` from here meant the FIRST target's close settled
the entire batch's grant with one fetch's cost and released the rest of
the hold; the other 199 fetches then ran against a reservation that no
longer existed. Accruing instead means the grant's settled total is the
SUM of its operations' observed costs, and the residual hold is returned
once, by whoever is entitled to declare the batch over: the call site
that minted the grant, or C3's lease sweeper after the ledger confirms
nothing is open under it.

The accrual runs only when this process's own close actually closed the
ledger row (``closed_at IS NULL`` -> ``NOW()``). That database CAS is the
accrual's idempotence: a redelivered close, or a recovery event replayed
by the next process's startup sweep, moves no counter a second time.

Both halves are best-effort *with respect to the ledger*: a C3 failure is
logged and, for settle, queued with the recovery event — it never rolls
back a ledger fact that already happened.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlsplit, urlunsplit

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app_shared.ids import new_uuid7
from app_shared.models.network_operations import (
    FRACTION_SCALE,
    NetworkOperation,
    NetworkOperationAllocation,
    NetworkTransport,
    allocate_cost_largest_remainder,
)
from app_shared.netledger.buffer import (
    KIND_CHILD_OPERATION,
    KIND_CLOSE_RECOVERY,
    BufferedEvent,
    DurableEventBuffer,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FlushReport",
    "LedgerCloseDeferred",
    "LedgerError",
    "LedgerOpenError",
    "NetLedgerRecorder",
    "OperationIntent",
    "OperationOutcome",
    "canonical_url_hash",
]

_ISO4217 = re.compile(r"^[A-Z]{3}$")


class LedgerError(RuntimeError):
    """Base for the two boundary failures this module distinguishes."""


class LedgerOpenError(LedgerError):
    """The ledger row could not be written. **Do not open the socket.**

    Raised by :meth:`NetLedgerRecorder.open`. A caller that catches this
    and dispatches anyway has defeated the entire point of C4: it is
    spending money with no record that it did.
    """


class LedgerCloseDeferred(LedgerError):
    """The close could not be written and was queued for the sweeper.

    Never raised out of :meth:`NetLedgerRecorder.close` — close is a
    non-raising call by contract. It exists so a caller that *wants* to
    know (a test, a health probe) can be told by inspecting the returned
    :class:`CloseReceipt` rather than by an exception it must not receive
    in production.
    """


def canonical_url_hash(url: str) -> str:
    """``sha256:<hex>`` over a normalized URL — the ledger's grouping key.

    Mirrors the scraping runtime's identity-adapter ``canonicalize_url``
    (lowercase scheme/host, collapsed and unquoted path, no trailing
    slash, no fragment). It is re-implemented rather than imported
    because ``app_shared`` is the lower layer and must never import the
    scraping library — the dependency runs one way only. The rules are
    kept byte-identical so an operation and the attempt that carried it
    group under the same key.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", unquote(parsed.path or "/"))
    if path != "/":
        path = path.rstrip("/")
    canonical = urlunsplit((parsed.scheme.lower(), f"{host}{port}", path, parsed.query, ""))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperationIntent:
    """Everything known BEFORE the socket opens. Immutable, written once.

    ``network_request_id`` is generated at construction, not at insert:
    C1's contract is that the operation has an identity before dispatch,
    so in-flight telemetry can be stamped with it without first
    round-tripping the INSERT — and so a process that dies mid-flight
    leaves a row that names the fetch it was making.

    ``workspace_id`` is the tenant that caused this fetch. It is NOT
    written to the operation row (that row is fleet-owned by design); it
    is the default allocation target at close. ``None`` means a fleet-side
    operation with no tenant to bill — a health probe, a robots.txt
    fetch — and such an operation simply gets no allocation rows.

    ``authorization_id`` is the C3 grant this dispatch was made under.
    """

    url: str
    domain: str
    transport: NetworkTransport
    provider: str
    workspace_id: uuid.UUID | None = None
    scrape_job_id: uuid.UUID | None = None
    authorization_id: uuid.UUID | None = None
    http_method: str = "GET"
    provider_account: str | None = None
    region: str | None = None
    entitlement_version: str | None = None
    budget_decision_version: str | None = None
    breaker_decision: str | None = None
    #: The physical operation this one RE-ATTEMPTS. A retry is its own
    #: physical fetch, never an overwrite of the one it retries.
    retry_parent_id: uuid.UUID | None = None
    #: The navigation that pulled this sub-resource in.
    parent_operation_id: uuid.UUID | None = None
    canonical_url_hash: str | None = None
    network_request_id: uuid.UUID = field(default_factory=new_uuid7)

    def hashed_url(self) -> str:
        return self.canonical_url_hash or canonical_url_hash(self.url)


@dataclass(frozen=True)
class OperationOutcome:
    """Everything known AFTER the transport is done. Written exactly once.

    Bytes are **transport-observed**, both compressed and decompressed,
    per B6's byte-truth work — the counters the scrapers already keep, not
    a parallel set invented here, and never provider-billed bytes (those
    are settlements, C5).

    ``allocations`` maps workspace id -> an integer WEIGHT (not a
    fraction, not a share of money). The recorder turns weights into
    exact ``fraction_ppb`` and ``allocated_cost_minor_units`` with
    largest-remainder rounding so both sums land exactly on
    :data:`FRACTION_SCALE` and on the operation's cost. ``None`` means
    "the intent's own workspace, at 1.0" — the ordinary single-tenant
    case, including the 5-match fan-out where five logical attempts ride
    one physical fetch for one workspace.
    """

    bytes_compressed: int | None = None
    bytes_decompressed: int | None = None
    response_status: int | None = None
    duration_ms: int | None = None
    failure_reason: str | None = None
    extraction_result: str | None = None
    identity_confidence: str | None = None
    comparability: str | None = None
    estimated_cost_minor_units: int | None = None
    currency: str | None = None
    billing_unit: str | None = None
    billing_rate_micro_units: int | None = None
    rate_effective_date: date | None = None
    allocations: Mapping[uuid.UUID, int] | None = None
    #: Accrue this outcome's observed cost against the operation's C3
    #: grant (``settle_partial``, NON-terminal — see that method and the
    #: costauth module's "One grant, MANY operations"). Left on by
    #: default: an operation dispatched under a grant that never accrues
    #: its cost leaves the whole batch's spend invisible to the budget
    #: until the lease expires and the hold is returned unspent.
    #:
    #: A grant may cover MANY operations, so this flag says only "this
    #: operation contributes its own cost"; it never terminates the
    #: grant. Turned OFF for the two shapes that would double-count: a
    #: browser SUB-RESOURCE (its parent's close already accrues the
    #: navigation, and the reservation was sized per top-level request,
    #: not per asset — the sub-resource's cost lives in its own ledger
    #: row and allocation, which is the authoritative per-tenant record),
    #: and an operation whose caller settles the grant itself in
    #: aggregate.
    settle_authorization: bool = True
    #: Dimensions C3 reserves that the ledger itself does not carry.
    browser_seconds: int = 0

    def __post_init__(self) -> None:
        for name in (
            "bytes_compressed",
            "bytes_decompressed",
            "estimated_cost_minor_units",
            "billing_rate_micro_units",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{name} must be an int or None, got {type(value)!r} — money and "
                    "byte counts are exact integers, never floats"
                )
            if value < 0:
                raise ValueError(f"{name} must be non-negative: {value!r}")
        if self.currency is not None and not _ISO4217.match(self.currency):
            raise ValueError(f"currency must be an ISO-4217 code: {self.currency!r}")
        if self.estimated_cost_minor_units is not None and self.currency is None:
            raise ValueError(
                "estimated_cost_minor_units requires a currency — C1's "
                "no_cost_requires_currency check rejects a bare amount"
            )


@dataclass(frozen=True)
class CloseReceipt:
    """What :meth:`NetLedgerRecorder.close` actually managed to do."""

    network_request_id: uuid.UUID
    written: bool
    deferred: bool
    settled: bool
    detail: str | None = None


@dataclass(frozen=True)
class FlushReport:
    """Outcome of one drain of the durable queue."""

    children_written: int = 0
    closes_recovered: int = 0
    failed: int = 0

    def __add__(self, other: "FlushReport") -> "FlushReport":
        return FlushReport(
            children_written=self.children_written + other.children_written,
            closes_recovered=self.closes_recovered + other.closes_recovered,
            failed=self.failed + other.failed,
        )

    @property
    def total(self) -> int:
        return self.children_written + self.closes_recovered


SessionScope = Callable[[], Any]


class NetLedgerRecorder:
    """The ONE writer of C1's ledger, living at the network boundary.

    Args:
        system_session_scope: zero-argument callable returning a context
            manager yielding a :class:`Session` on the sanctioned
            BYPASSRLS system seam. Defaults to
            :func:`app_shared.database.get_system_session`, imported
            lazily so this module stays importable (and unit-testable)
            with no DSN configured at all.
        buffer: the host's durable local queue. Constructed on first use
            from :data:`~app_shared.netledger.buffer.BUFFER_PATH_ENV`
            when not supplied, so a caller that never buffers never pays
            for the file.
        costauth: the C3 :class:`CostAuthorizationService` this recorder
            heartbeats and settles through. ``None`` disables both — used
            by fleet-side operations that were never authorized (there is
            no grant to renew) and by tests.
        now: injectable clock returning an aware UTC datetime.
    """

    def __init__(
        self,
        system_session_scope: SessionScope | None = None,
        *,
        buffer: DurableEventBuffer | None = None,
        costauth: Any | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._system_session_scope = system_session_scope
        self._buffer = buffer
        self._costauth = costauth
        self._now = now or (lambda: datetime.now(timezone.utc))
        #: Intents of operations opened by THIS process and not yet
        #: closed. Lets close/heartbeat recover the authorization and the
        #: default allocation target without a read-back. A process
        #: restart empties it; close then reads the authorization from
        #: the row itself, so this is a fast path, never the source of
        #: truth.
        self._open_intents: dict[uuid.UUID, OperationIntent] = {}

    # -- plumbing -----------------------------------------------------------

    @property
    def open_operations(self) -> tuple[uuid.UUID, ...]:
        """Operations opened by THIS process and not yet closed.

        The heartbeat loop's work list, and the set the boundary closes
        as ``UNTERMINATED_AT_SPIDER_CLOSE`` at shutdown so no row is left
        claiming to still be in flight forever.
        """
        return tuple(self._open_intents)

    @property
    def buffer(self) -> DurableEventBuffer:
        if self._buffer is None:
            self._buffer = DurableEventBuffer()
        return self._buffer

    def _session(self) -> Any:
        scope = self._system_session_scope
        if scope is None:
            from app_shared.database import get_system_session

            scope = get_system_session
        return scope()

    # -- the contract -------------------------------------------------------

    def open(self, intent: OperationIntent) -> uuid.UUID:
        """Write the intent row and return its ``network_request_id``.

        Runs BEFORE the socket opens. Raises :class:`LedgerOpenError` if
        the row could not be written — the caller MUST then abandon the
        dispatch. There is deliberately no buffered/degraded open path:
        an operation whose identity only exists locally cannot be
        referenced by the `request_attempts` row the same fetch produces,
        and a ledger that sometimes fabricates identities is not a
        ledger.
        """
        row = {
            "id": uuid.uuid4(),
            "network_request_id": intent.network_request_id,
            "retry_parent_id": intent.retry_parent_id,
            "parent_operation_id": intent.parent_operation_id,
            "scrape_job_id": intent.scrape_job_id,
            "canonical_url_hash": intent.hashed_url(),
            "domain": intent.domain,
            "http_method": intent.http_method,
            "region": intent.region,
            "provider": intent.provider,
            "provider_account": intent.provider_account,
            "transport": NetworkTransport(intent.transport),
            "authorization_id": intent.authorization_id,
            "entitlement_version": intent.entitlement_version,
            "budget_decision_version": intent.budget_decision_version,
            "breaker_decision": intent.breaker_decision,
            "created_at": self._now(),
        }
        try:
            with self._session() as session:
                session.execute(insert(NetworkOperation).values(**row))
                session.commit()
        except Exception as exc:  # noqa: BLE001 - re-raised as the fail-closed signal
            logger.error(
                "netledger.open_failed network_request_id=%s domain=%s transport=%s: %s",
                intent.network_request_id,
                intent.domain,
                intent.transport,
                exc,
            )
            raise LedgerOpenError(
                f"could not open ledger row for {intent.domain} "
                f"({intent.transport}); the dispatch must not proceed"
            ) from exc

        self._open_intents[intent.network_request_id] = intent
        return intent.network_request_id

    def close(
        self, network_request_id: uuid.UUID | str, outcome: OperationOutcome
    ) -> CloseReceipt:
        """Write the outcome exactly once. **Never raises.**

        A database failure here is queued as a ``CLOSE_RECOVERY`` event
        and replayed by :meth:`sweep`; the returned receipt says which
        happened. See the module docstring for why this half is not
        symmetric with :meth:`open`.
        """
        nrid = _as_uuid(network_request_id)
        intent = self._open_intents.get(nrid)
        try:
            newly_closed = self._write_close(nrid, outcome, intent)
        except Exception as exc:  # noqa: BLE001 - close never propagates
            detail = f"{type(exc).__name__}: {exc}"
            logger.error(
                "netledger.close_failed network_request_id=%s (queued for sweeper): %s",
                nrid,
                detail,
            )
            try:
                self.buffer.append(
                    KIND_CLOSE_RECOVERY, _close_payload(nrid, outcome, intent)
                )
            except Exception:  # noqa: BLE001 - last resort; nothing else can hold it
                logger.critical(
                    "netledger.close_unrecoverable network_request_id=%s — the durable "
                    "buffer also refused the event; these bytes are NOT recorded",
                    nrid,
                    exc_info=True,
                )
                return CloseReceipt(nrid, written=False, deferred=False, settled=False,
                                    detail=detail)
            return CloseReceipt(nrid, written=False, deferred=True, settled=False,
                                detail=detail)

        self._open_intents.pop(nrid, None)
        # `newly_closed` is the IDEMPOTENCE TOKEN for the C3 accrual.
        # `settle_partial` accumulates rather than compare-and-sets, so
        # something has to guarantee a redelivered/replayed close accrues
        # once — and the ledger's own `closed_at IS NULL` update is
        # already exactly that guarantee, decided by the database. Reusing
        # it means the money and the ledger row can never disagree about
        # whether this operation happened.
        settled = self._settle(nrid, outcome, intent) if newly_closed else False
        return CloseReceipt(nrid, written=True, deferred=False, settled=settled)

    @property
    def open_authorizations(self) -> tuple[uuid.UUID, ...]:
        """The DISTINCT C3 grants this process has live operations under.

        A batch grant covers up to a few hundred operations, so the
        heartbeat loop must renew per GRANT, not per operation — otherwise
        one tick is N identical round trips on the tenant seam for the one
        lease they all share.
        """
        seen: dict[uuid.UUID, None] = {}
        for intent in self._open_intents.values():
            if intent.authorization_id is not None:
                seen.setdefault(_as_uuid(intent.authorization_id), None)
        return tuple(seen)

    def heartbeat(self, network_request_id: uuid.UUID | str) -> bool:
        """Renew the C3 lease while this operation is still LIVE.

        The reason this exists at the boundary rather than at the call
        site: a browser navigation can outlive C3's lease, and the only
        component that knows the fetch is still running is the one
        holding the socket. Returns ``False`` when there is nothing to
        renew (no C3 service wired, or the operation carries no
        authorization) — that is a fleet operation, not an error.
        """
        nrid = _as_uuid(network_request_id)
        if self._costauth is None:
            return False
        authorization_id = self._authorization_for(nrid)
        if authorization_id is None:
            return False
        return self.heartbeat_authorization(authorization_id)

    def heartbeat_authorization(self, authorization_id: uuid.UUID | str) -> bool:
        """Renew one C3 grant's lease directly, by grant id.

        The form the boundary's heartbeat loop wants: a grant covering a
        batch has many live operations sharing ONE lease, so the loop
        renews per :attr:`open_authorizations` entry rather than per open
        operation.
        """
        auth_id = _as_uuid(authorization_id)
        if self._costauth is None:
            return False
        try:
            self._costauth.heartbeat(auth_id)
        except Exception:  # noqa: BLE001 - a lease renewal failure must never
            # kill the in-flight fetch it was merely extending; C3's own
            # sweeper reclaims an expired lease.
            logger.warning(
                "netledger.heartbeat_failed authorization_id=%s",
                auth_id,
                exc_info=True,
            )
            return False
        return True

    # -- browser sub-resources: buffered, not inserted -----------------------

    def buffer_child(
        self, intent: OperationIntent, outcome: OperationOutcome
    ) -> uuid.UUID:
        """Queue one sub-resource as a child operation. Cheap and local.

        The child is fully described here — it is opened AND closed in a
        single ledger write when the queue is flushed, because by the
        time the interception hook sees it the fetch is already over.
        ``parent_operation_id`` must be the document operation's
        ``network_request_id``.
        """
        if intent.parent_operation_id is None:
            raise ValueError(
                "a buffered child operation must name its parent_operation_id — a "
                "sub-resource with no navigation to belong to is an orphan cost"
            )
        self.buffer.append(
            KIND_CHILD_OPERATION,
            {
                "intent": _intent_payload(intent),
                "outcome": _outcome_payload(outcome),
            },
        )
        return intent.network_request_id

    def buffer_children(
        self, items: Sequence[tuple[OperationIntent, OperationOutcome]]
    ) -> list[uuid.UUID]:
        """Queue a burst of sub-resources in ONE local commit."""
        for intent, _ in items:
            if intent.parent_operation_id is None:
                raise ValueError(
                    "a buffered child operation must name its parent_operation_id"
                )
        self.buffer.append_many(
            (
                KIND_CHILD_OPERATION,
                {"intent": _intent_payload(i), "outcome": _outcome_payload(o)},
            )
            for i, o in items
        )
        return [intent.network_request_id for intent, _ in items]

    def flush(self, *, limit: int = 500, max_batches: int = 100) -> FlushReport:
        """Drain the durable queue into the ledger, in batches.

        Each event is written in its own transaction and deleted from the
        queue only AFTER that transaction commits, so a kill between the
        two replays rather than loses. Replay is idempotent: a child
        carries its own unique ``network_request_id`` (a duplicate insert
        is skipped), and a close recovery targets an operation whose
        ``closed_at`` C1's trigger permits to be written once.
        """
        report = FlushReport()
        for _ in range(max_batches):
            events = self.buffer.pending(limit=limit)
            if not events:
                break
            batch = self._flush_batch(events)
            report = report + batch
            if batch.total == 0:
                # Nothing in this batch could be written; stop rather than
                # spin on the same head-of-queue entries.
                break
        return report

    def sweep(self, *, limit: int = 500, max_batches: int = 100) -> FlushReport:
        """Startup sweep: replay whatever the previous process left behind.

        Identical to :meth:`flush`; named separately because the call
        SITE is what matters — running this on spider start is what makes
        "process exit cannot lose events" true rather than aspirational.
        """
        pending = self.buffer.pending_count()
        if pending:
            logger.info("netledger.startup_sweep pending=%d", pending)
        return self.flush(limit=limit, max_batches=max_batches)

    # -- internals ----------------------------------------------------------

    def _flush_batch(self, events: Sequence[BufferedEvent]) -> FlushReport:
        children = 0
        closes = 0
        failed = 0
        done: list[int] = []
        for event in events:
            try:
                if event.kind == KIND_CHILD_OPERATION:
                    self._write_child(event.payload)
                    children += 1
                elif event.kind == KIND_CLOSE_RECOVERY:
                    self._replay_close(event.payload)
                    closes += 1
                else:
                    logger.warning(
                        "netledger.unknown_buffer_kind kind=%s row_id=%s",
                        event.kind,
                        event.row_id,
                    )
                    self.buffer.defer(event.row_id, f"unknown kind {event.kind}")
                    failed += 1
                    continue
                done.append(event.row_id)
            except Exception as exc:  # noqa: BLE001 - one bad event must not
                # strand the rest of the queue behind it.
                failed += 1
                self.buffer.defer(event.row_id, f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "netledger.flush_event_failed row_id=%s kind=%s: %s",
                    event.row_id,
                    event.kind,
                    exc,
                )
        if done:
            self.buffer.resolve(done)
        return FlushReport(children_written=children, closes_recovered=closes, failed=failed)

    def _write_child(self, payload: Mapping[str, Any]) -> None:
        intent = _intent_from_payload(payload["intent"])
        outcome = _outcome_from_payload(payload["outcome"])
        with self._session() as session:
            existing = session.execute(
                select(NetworkOperation.network_request_id).where(
                    NetworkOperation.network_request_id == intent.network_request_id
                )
            ).first()
            if existing is None:
                session.execute(
                    insert(NetworkOperation).values(
                        id=uuid.uuid4(),
                        network_request_id=intent.network_request_id,
                        retry_parent_id=intent.retry_parent_id,
                        parent_operation_id=intent.parent_operation_id,
                        scrape_job_id=intent.scrape_job_id,
                        canonical_url_hash=intent.hashed_url(),
                        domain=intent.domain,
                        http_method=intent.http_method,
                        region=intent.region,
                        provider=intent.provider,
                        provider_account=intent.provider_account,
                        transport=NetworkTransport(intent.transport),
                        authorization_id=intent.authorization_id,
                        entitlement_version=intent.entitlement_version,
                        budget_decision_version=intent.budget_decision_version,
                        breaker_decision=intent.breaker_decision,
                    )
                )
            self._apply_close(session, intent.network_request_id, outcome, intent)
            session.commit()

    def _replay_close(self, payload: Mapping[str, Any]) -> None:
        nrid = _as_uuid(payload["network_request_id"])
        outcome = _outcome_from_payload(payload["outcome"])
        intent_payload = payload.get("intent")
        intent = _intent_from_payload(intent_payload) if intent_payload else None
        # Same idempotence token as `close`: a recovery event replayed
        # twice (this process, then the next process's startup sweep)
        # closes the row once and therefore accrues its cost once.
        if self._write_close(nrid, outcome, intent):
            self._settle(nrid, outcome, intent)

    def _write_close(
        self,
        nrid: uuid.UUID,
        outcome: OperationOutcome,
        intent: OperationIntent | None,
    ) -> bool:
        """Apply the close in its own transaction. ``True`` iff it CLOSED
        the row (``False`` = it was already closed — a replay)."""
        with self._session() as session:
            newly_closed = self._apply_close(session, nrid, outcome, intent)
            session.commit()
        return newly_closed

    def _apply_close(
        self,
        session: Session,
        nrid: uuid.UUID,
        outcome: OperationOutcome,
        intent: OperationIntent | None,
    ) -> bool:
        result = session.execute(
            update(NetworkOperation)
            .where(
                NetworkOperation.network_request_id == nrid,
                NetworkOperation.closed_at.is_(None),
            )
            .values(
                closed_at=self._now(),
                bytes_compressed=outcome.bytes_compressed,
                bytes_decompressed=outcome.bytes_decompressed,
                response_status=outcome.response_status,
                duration_ms=outcome.duration_ms,
                failure_reason=outcome.failure_reason,
                extraction_result=outcome.extraction_result,
                identity_confidence=outcome.identity_confidence,
                comparability=outcome.comparability,
                estimated_cost_minor_units=outcome.estimated_cost_minor_units,
                currency=outcome.currency,
                billing_unit=outcome.billing_unit,
                billing_rate_micro_units=outcome.billing_rate_micro_units,
                rate_effective_date=outcome.rate_effective_date,
            )
        )
        if result.rowcount == 0:
            # Either the row is already closed (a replayed close — the
            # correct, idempotent no-op) or it does not exist at all (a
            # genuine loss the sweeper must keep retrying). Only the
            # second is an error.
            exists = session.execute(
                select(NetworkOperation.closed_at).where(
                    NetworkOperation.network_request_id == nrid
                )
            ).first()
            if exists is None:
                raise LedgerError(
                    f"no network_operations row for {nrid} — cannot close an "
                    "operation that was never opened"
                )
            logger.debug("netledger.close_noop network_request_id=%s (already closed)", nrid)
            return False
        self._write_allocations(session, nrid, outcome, intent)
        return True

    def _write_allocations(
        self,
        session: Session,
        nrid: uuid.UUID,
        outcome: OperationOutcome,
        intent: OperationIntent | None,
    ) -> None:
        """Split the operation's cost across the workspaces that caused it.

        Skipped entirely when there is no cost (an operation still being
        priced, or one with nothing to bill) or no workspace (a fleet
        probe): C1's deferred trigger skips a NULL-cost operation, and an
        allocation with nothing to allocate is noise, not a fact.
        """
        cost = outcome.estimated_cost_minor_units
        if cost is None or outcome.currency is None:
            return
        weights = dict(outcome.allocations or {})
        if not weights and intent is not None and intent.workspace_id is not None:
            weights = {intent.workspace_id: 1}
        if not weights:
            return

        workspaces = [_as_uuid(ws) for ws in weights]
        raw = [int(weights[ws]) for ws in weights]
        fractions = allocate_cost_largest_remainder(FRACTION_SCALE, raw)
        amounts = allocate_cost_largest_remainder(cost, raw)
        session.execute(
            insert(NetworkOperationAllocation),
            [
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "operation_id": nrid,
                    "fraction_ppb": fraction,
                    "allocated_cost_minor_units": amount,
                    "currency": outcome.currency,
                }
                for workspace_id, fraction, amount in zip(
                    workspaces, fractions, amounts, strict=True
                )
            ],
        )

    def _settle(
        self,
        nrid: uuid.UUID,
        outcome: OperationOutcome,
        intent: OperationIntent | None,
    ) -> bool:
        """Accrue THIS operation's observed cost against its C3 grant.

        ``settle_partial``, never ``settle``: one grant covers a whole
        batch of operations (see the costauth module's "One grant, MANY
        operations"), so the network boundary — which sees one operation
        at a time and can never know it is holding the last one — has no
        business terminating the grant. It contributes this operation's
        cost and leaves the hold live for the rest of the batch. The
        terminal close belongs to the site that minted the grant, or, for
        a batch POSTed to a remote node, to C3's lease sweeper once the
        ledger confirms nothing is open under it any more.

        Called ONLY when this process's close actually closed the row, so
        the accrual happens exactly once per operation.
        """
        if not outcome.settle_authorization or self._costauth is None:
            return False
        authorization_id = (
            intent.authorization_id if intent is not None else None
        ) or self._authorization_for(nrid)
        if authorization_id is None:
            return False
        from app_shared.costauth import SettledCost

        bytes_used = outcome.bytes_compressed
        if bytes_used is None:
            bytes_used = outcome.bytes_decompressed or 0
        try:
            self._costauth.settle_partial(
                authorization_id,
                SettledCost(
                    cost_minor_units=int(outcome.estimated_cost_minor_units or 0),
                    bytes_used=int(bytes_used),
                    requests=1,
                    browser_seconds=int(outcome.browser_seconds),
                ),
            )
        except Exception:  # noqa: BLE001 - the ledger fact is already written; a
            # settle failure must not undo it. C3's lease sweeper reclaims
            # the reservation once the lease expires.
            logger.warning(
                "netledger.settle_failed network_request_id=%s authorization_id=%s",
                nrid,
                authorization_id,
                exc_info=True,
            )
            return False
        return True

    def _authorization_for(self, nrid: uuid.UUID) -> uuid.UUID | None:
        intent = self._open_intents.get(nrid)
        if intent is not None:
            return intent.authorization_id
        try:
            with self._session() as session:
                row = session.execute(
                    select(NetworkOperation.authorization_id).where(
                        NetworkOperation.network_request_id == nrid
                    )
                ).first()
        except Exception:  # noqa: BLE001 - a read-back failure is not fatal to
            # the fetch; it only means no lease renewal this round.
            logger.debug("netledger.authorization_lookup_failed nrid=%s", nrid, exc_info=True)
            return None
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Payload (de)serialization for the durable queue
# ---------------------------------------------------------------------------


def _intent_payload(intent: OperationIntent) -> dict[str, Any]:
    return {
        "url": intent.url,
        "domain": intent.domain,
        "transport": str(intent.transport),
        "provider": intent.provider,
        "workspace_id": intent.workspace_id,
        "scrape_job_id": intent.scrape_job_id,
        "authorization_id": intent.authorization_id,
        "http_method": intent.http_method,
        "provider_account": intent.provider_account,
        "region": intent.region,
        "entitlement_version": intent.entitlement_version,
        "budget_decision_version": intent.budget_decision_version,
        "breaker_decision": intent.breaker_decision,
        "retry_parent_id": intent.retry_parent_id,
        "parent_operation_id": intent.parent_operation_id,
        "canonical_url_hash": intent.hashed_url(),
        "network_request_id": intent.network_request_id,
    }


def _intent_from_payload(payload: Mapping[str, Any]) -> OperationIntent:
    return OperationIntent(
        url=payload["url"],
        domain=payload["domain"],
        transport=NetworkTransport(payload["transport"]),
        provider=payload["provider"],
        workspace_id=_opt_uuid(payload.get("workspace_id")),
        scrape_job_id=_opt_uuid(payload.get("scrape_job_id")),
        authorization_id=_opt_uuid(payload.get("authorization_id")),
        http_method=payload.get("http_method", "GET"),
        provider_account=payload.get("provider_account"),
        region=payload.get("region"),
        entitlement_version=payload.get("entitlement_version"),
        budget_decision_version=payload.get("budget_decision_version"),
        breaker_decision=payload.get("breaker_decision"),
        retry_parent_id=_opt_uuid(payload.get("retry_parent_id")),
        parent_operation_id=_opt_uuid(payload.get("parent_operation_id")),
        canonical_url_hash=payload.get("canonical_url_hash"),
        network_request_id=_as_uuid(payload["network_request_id"]),
    )


def _outcome_payload(outcome: OperationOutcome) -> dict[str, Any]:
    return {
        "bytes_compressed": outcome.bytes_compressed,
        "bytes_decompressed": outcome.bytes_decompressed,
        "response_status": outcome.response_status,
        "duration_ms": outcome.duration_ms,
        "failure_reason": outcome.failure_reason,
        "extraction_result": outcome.extraction_result,
        "identity_confidence": outcome.identity_confidence,
        "comparability": outcome.comparability,
        "estimated_cost_minor_units": outcome.estimated_cost_minor_units,
        "currency": outcome.currency,
        "billing_unit": outcome.billing_unit,
        "billing_rate_micro_units": outcome.billing_rate_micro_units,
        "rate_effective_date": outcome.rate_effective_date,
        "allocations": (
            {str(k): int(v) for k, v in outcome.allocations.items()}
            if outcome.allocations
            else None
        ),
        "settle_authorization": outcome.settle_authorization,
        "browser_seconds": outcome.browser_seconds,
    }


def _outcome_from_payload(payload: Mapping[str, Any]) -> OperationOutcome:
    allocations = payload.get("allocations")
    rate_effective_date = payload.get("rate_effective_date")
    return OperationOutcome(
        bytes_compressed=payload.get("bytes_compressed"),
        bytes_decompressed=payload.get("bytes_decompressed"),
        response_status=payload.get("response_status"),
        duration_ms=payload.get("duration_ms"),
        failure_reason=payload.get("failure_reason"),
        extraction_result=payload.get("extraction_result"),
        identity_confidence=payload.get("identity_confidence"),
        comparability=payload.get("comparability"),
        estimated_cost_minor_units=payload.get("estimated_cost_minor_units"),
        currency=payload.get("currency"),
        billing_unit=payload.get("billing_unit"),
        billing_rate_micro_units=payload.get("billing_rate_micro_units"),
        rate_effective_date=(
            date.fromisoformat(rate_effective_date) if rate_effective_date else None
        ),
        allocations=(
            {_as_uuid(k): int(v) for k, v in allocations.items()} if allocations else None
        ),
        settle_authorization=bool(payload.get("settle_authorization", True)),
        browser_seconds=int(payload.get("browser_seconds", 0)),
    )


def _close_payload(
    nrid: uuid.UUID, outcome: OperationOutcome, intent: OperationIntent | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "network_request_id": nrid,
        "outcome": _outcome_payload(outcome),
    }
    if intent is not None:
        payload["intent"] = _intent_payload(intent)
    return payload


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _opt_uuid(value: Any) -> uuid.UUID | None:
    return None if value is None else _as_uuid(value)
