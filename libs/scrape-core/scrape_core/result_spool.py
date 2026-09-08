"""Durable on-disk spool for scrape results awaiting persistence (EPA F05, plan task B1).

Why this exists
---------------
``BatchedPersistencePipeline`` used to hold every not-yet-flushed
``ScrapeResult`` in a plain Python list. That list lives in the spider
process, so a flush that failed (a Postgres restart, a connection reset,
a container receiving SIGKILL mid-run) lost the batch outright: the money
for those fetches had already been spent, the pages had already been
fetched, and the only record of them disappeared with the process. The
old ``_on_flush_failure`` said so in as many words -- *"the affected items
are lost from this flush (no retry queue in this MVP slice)"*.

This module is that retry queue, made durable. Every item is written to a
local SQLite file **before** it enters the in-memory buffer, and its row
is deleted only once the database transaction that persisted it has
COMMITTED. A kill anywhere in between replays the item rather than losing
it; the replay is safe because the persistence writes are idempotent by
``attempt_uuid`` (see the ``observation_attempt_identity`` migration and
``scrape_core.pipelines._flush_batch``).

Why it wraps ``DurableEventBuffer``
-----------------------------------
The scraper host already runs exactly this primitive for the network
ledger: :class:`app_shared.netledger.buffer.DurableEventBuffer`, an
append-only SQLite/WAL queue with ``append_many``/``pending``/``resolve``/
``defer`` and a documented durability claim (a COMMIT survives the
*process* dying, which is the failure this spool exists for). Reusing it
means one crash-safety story on the host instead of two, and one file
format for an operator to inspect. This module adds only what a
``ScrapeResult`` needs on top: a JSON codec for the dataclass (the
buffer's own ``json_default`` deliberately refuses ``Decimal`` and enum
values), a quarantine kind for batches that have failed too many times,
and a schema version of its own.

Durability, stated precisely
----------------------------
Inherited verbatim from ``DurableEventBuffer``: with ``journal_mode=WAL``
and ``synchronous=NORMAL`` a COMMIT is written into the WAL with
``write(2)`` before it returns, so a committed row survives the process
being killed. It does NOT survive an OS crash or power loss between
commit and checkpoint; pass ``synchronous="FULL"`` to buy that at the
price of an fsync per append. The default matches the ledger buffer's.

Quarantine
----------
A batch that keeps failing must not retry forever -- an unparseable
payload or a row Postgres will never accept would otherwise occupy the
retry loop indefinitely and keep the spool from draining. After
``Settings.SCRAPE_FLUSH_QUARANTINE_AFTER`` failed attempts a row is
*moved* to ``kind='quarantined'``: still on disk, still readable by an
operator, but no longer returned by :meth:`ResultSpool.pending` and so
never replayed automatically. Moving (rather than deleting) is the whole
point -- a quarantined result is evidence of money already spent, and
deleting it would reintroduce exactly the silent loss this module
removes.
"""

from __future__ import annotations

import enum
import logging
import os
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence, get_args, get_origin, get_type_hints

from app_shared.netledger.buffer import BufferedEvent, DurableEventBuffer

from scrape_core.items import ScrapeResult

logger = logging.getLogger(__name__)

__all__ = [
    "KIND_QUARANTINED",
    "KIND_SCRAPE_RESULT",
    "SPOOL_SCHEMA_VERSION",
    "ResultSpool",
    "SpooledBatch",
]

#: A scrape result durably queued for persistence, not yet committed.
KIND_SCRAPE_RESULT = "SCRAPE_RESULT"

#: A scrape result whose flush failed ``SCRAPE_FLUSH_QUARANTINE_AFTER``
#: times. Kept on disk, never replayed automatically. The literal string
#: is deliberately lower-case: it is what an operator greps for in the
#: spool file, and it is the value the plan names.
KIND_QUARANTINED = "quarantined"

#: Schema version of the payload envelope this module writes.
#:
#: Bump whenever the encoded shape changes in a way a running spool
#: drainer would notice -- a renamed ``ScrapeResult`` field, a changed
#: encoding for a value type, a new envelope key. It exists so a release
#: can be identified against the spool files it will find on scraper
#: hosts: ``scripts/write_release_manifest.py`` records it under
#: ``buffer_versions.scrape_result_spool_version``, and
#: :meth:`ResultSpool.pending` refuses (rather than mis-decodes) a row
#: written by a version it does not know.
#:
#: Version 1 is the original envelope: ``{"v": 1, "result": {...}}`` with
#: one row per ``ScrapeResult``.
SPOOL_SCHEMA_VERSION = 1

_ENVELOPE_VERSION_KEY = "v"
_ENVELOPE_RESULT_KEY = "result"


@dataclass(frozen=True)
class SpooledBatch:
    """One durably queued ``ScrapeResult``, as handed back to the flusher.

    Named for the unit the *flusher* works in (the plan's interface says
    ``pending(limit) -> list[SpooledBatch]``) even though one instance
    carries one result: the pipeline re-groups these into flush batches by
    ``workspace_id``, because a batch is a transaction boundary and a
    transaction is workspace-scoped. Row-per-result is what makes partial
    progress expressible -- a batch of 50 whose 50 inserts committed
    resolves 50 ids, and a batch that failed defers exactly the ids that
    did not.
    """

    row_id: int
    workspace_id: uuid.UUID
    result: ScrapeResult
    attempts: int


class ResultSpool:
    """Durable queue of ``ScrapeResult`` items awaiting persistence.

    Thread-safety is inherited from :class:`DurableEventBuffer` (one
    connection guarded by a lock), so the reactor thread may append while
    a thread-pool thread resolves.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        synchronous: str = "NORMAL",
    ) -> None:
        self._buffer = DurableEventBuffer(path, synchronous=synchronous)

    @property
    def path(self) -> str:
        return self._buffer.path

    # -- writing ------------------------------------------------------------

    def append_batch(self, results: Sequence[ScrapeResult]) -> list[int]:
        """Durably append ``results`` in ONE transaction; returns their row ids.

        Returned ids are positional with ``results``. The caller keeps
        them alongside its in-memory buffer so a later flush can resolve
        exactly the rows it committed.
        """
        return self._buffer.append_many(
            (KIND_SCRAPE_RESULT, _encode(result)) for result in results
        )

    # -- reading / draining -------------------------------------------------

    def pending(self, limit: int = 500) -> list[SpooledBatch]:
        """The oldest ``limit`` unpersisted results, oldest first.

        Quarantined rows are excluded by construction (they carry a
        different ``kind``), so a drainer can loop on this method without
        ever re-picking a batch that has already exhausted its retries.
        """
        return self._decode_events(self._buffer.pending(limit, kind=KIND_SCRAPE_RESULT))

    def pending_count(self) -> int:
        """How many results are still queued (quarantined rows excluded)."""
        return self._buffer.pending_count(kind=KIND_SCRAPE_RESULT)

    def quarantined_count(self) -> int:
        return self._buffer.pending_count(kind=KIND_QUARANTINED)

    def load(self, row_ids: Sequence[int]) -> list[SpooledBatch]:
        """The queued results for ``row_ids``, oldest first.

        Rows that are gone (already resolved, or quarantined by a
        concurrent drainer) are simply absent from the result -- asking
        for a row that no longer needs persisting is not an error.
        """
        wanted = {int(row_id) for row_id in row_ids}
        if not wanted:
            return []
        return [entry for entry in self.pending(limit=_scan_limit(wanted)) if entry.row_id in wanted]

    # -- completing ---------------------------------------------------------

    def resolve(self, row_ids: Sequence[int]) -> None:
        """Delete rows whose persistence transaction has COMMITTED.

        Called only *after* the commit, never before: a kill in between
        replays the results (idempotent by ``attempt_uuid``), where the
        reverse order would lose them.
        """
        self._buffer.resolve(row_ids)

    def defer(self, row_id: int, error: str) -> int:
        """Record one failed flush attempt; the row STAYS queued.

        Returns the row's new attempt count, which is what the caller
        compares against ``SCRAPE_FLUSH_QUARANTINE_AFTER``. A row that has
        vanished (resolved by a concurrent drainer) reports ``0``.
        """
        return self.defer_many([row_id], error).get(int(row_id), 0)

    def defer_many(self, row_ids: Sequence[int], error: str) -> dict[int, int]:
        """:meth:`defer` for a whole batch; returns ``{row_id: attempts}``.

        One read for the whole batch rather than one per row -- a failed
        flush of 50 items would otherwise re-scan the spool 50 times on
        the very path where the database is already unhappy.
        """
        ids = [int(row_id) for row_id in row_ids]
        if not ids:
            return {}
        for row_id in ids:
            self._buffer.defer(row_id, error)
        wanted = set(ids)
        return {
            entry.row_id: entry.attempts
            for entry in self.pending(limit=_scan_limit(wanted))
            if entry.row_id in wanted
        }

    def quarantine(self, row_ids: Sequence[int]) -> list[int]:
        """Move rows out of the retry loop, keeping them on disk.

        Implemented as append-then-delete through the buffer's public API
        (a re-append under ``kind='quarantined'``, then a ``resolve`` of
        the original row) rather than an in-place ``UPDATE``: the buffer
        owns its own schema and its own connection, and this module has
        no business reaching past it. The re-append commits before the
        delete, so a kill in between leaves a duplicate quarantined row --
        the safe direction, since the alternative loses the evidence.

        Returns the ids of the NEW quarantined rows.
        """
        entries = self.load(row_ids)
        if not entries:
            return []
        new_ids = self._buffer.append_many(
            (KIND_QUARANTINED, _encode(entry.result)) for entry in entries
        )
        self._buffer.resolve([entry.row_id for entry in entries])
        return new_ids

    def close(self) -> None:
        self._buffer.close()

    # -- internals ----------------------------------------------------------

    def _decode_events(self, events: Iterable[BufferedEvent]) -> list[SpooledBatch]:
        decoded: list[SpooledBatch] = []
        for event in events:
            payload = event.payload
            version = payload.get(_ENVELOPE_VERSION_KEY)
            if version != SPOOL_SCHEMA_VERSION:
                # A row written by a schema this build does not know
                # cannot be replayed correctly. Left in place with the
                # reason recorded (never deleted) so it stays visible to
                # an operator and to a later build that does know it.
                self._buffer.defer(event.row_id, f"unknown spool schema version {version!r}")
                continue
            try:
                result = _decode(payload[_ENVELOPE_RESULT_KEY])
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the drain
                self._buffer.defer(event.row_id, f"undecodable scrape result: {exc}")
                continue
            decoded.append(
                SpooledBatch(
                    row_id=event.row_id,
                    workspace_id=result.workspace_id,
                    result=result,
                    attempts=event.attempts,
                )
            )
        return decoded


def _scan_limit(wanted: set[int]) -> int:
    """A LIMIT that certainly covers ``wanted``.

    Row ids are monotonic, so the largest wanted id bounds how many rows
    can precede it; the spool is bounded by
    ``SCRAPE_FLUSH_MAX_PENDING_BATCHES x SCRAPE_FLUSH_MAX_ITEMS`` in
    normal operation, so this is a small number in practice and a correct
    one in the pathological case.
    """
    return max(wanted) if wanted else 0


# --- codec -------------------------------------------------------------------
#
# `ScrapeResult` is a plain dataclass of UUIDs, datetimes, Decimals,
# enums, and primitives. The ledger buffer's own `json_default` refuses
# Decimal and enum values on purpose (it never carries them), so the
# encoding lives here: values are lowered to JSON scalars on the way in
# and raised back to their declared types on the way out, driven by the
# dataclass's own annotations rather than a hand-maintained field list --
# a field added to `ScrapeResult` is spooled correctly without touching
# this module.


#: EPA C5 (F19). Fields that are DELIBERATELY not spooled -- written as
#: an explicit `null` so `_decode` restores the dataclass default rather
#: than tripping over a type this codec does not speak.
#:
#: The spool exists to make the OBSERVATION durable across a crash
#: between buffering and COMMIT. Neither of these is part of that row's
#: substance:
#:
#:   `raw_evidence` is a whole competitor page. Base64ing every page into
#:       a local SQLite file would triple the on-disk cost of a fan-out
#:       (one copy per sibling riding the same fetch) for bytes whose
#:       durable home is the content-addressed evidence store -- and a
#:       replay only happens when the flush that would have STORED them
#:       failed, so the blob would not exist to be referenced anyway.
#:   `offer` is a pydantic model, not a JSON scalar. Lowering it here
#:       would put a second, divergent serializer for the
#:       `OfferObservation` contract in a module whose job is a crash
#:       spool.
#:
#: A replayed batch therefore writes the same price with a NULL offer
#: projection and a NULL evidence hash. That is a real (small) loss of
#: enrichment on an already-rare path, and it is the honest outcome:
#: NULL says "this replayed row has no evidence", which is true.
_NON_SPOOLED_FIELDS = frozenset({"offer", "raw_evidence"})


def _encode(result: ScrapeResult) -> dict[str, Any]:
    return {
        _ENVELOPE_VERSION_KEY: SPOOL_SCHEMA_VERSION,
        _ENVELOPE_RESULT_KEY: {
            field.name: (
                None
                if field.name in _NON_SPOOLED_FIELDS
                else _encode_value(getattr(result, field.name))
            )
            for field in fields(result)
        },
    }


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        # str(), never float(): a price that round-trips through binary
        # floating point is a price that changed.
        return str(value)
    raise TypeError(f"result spool cannot serialize {type(value)!r}")


def _decode(payload: dict[str, Any]) -> ScrapeResult:
    decoders = _field_decoders()
    kwargs: dict[str, Any] = {}
    for name, decode in decoders.items():
        if name not in payload:
            # A field this build knows and the writer did not: the
            # dataclass default applies. Only reachable across a rolling
            # deploy, and only for fields that have a default.
            continue
        raw = payload[name]
        kwargs[name] = None if raw is None else decode(raw)
    return ScrapeResult(**kwargs)


_FIELD_DECODERS: dict[str, Any] | None = None


def _field_decoders() -> dict[str, Any]:
    global _FIELD_DECODERS
    if _FIELD_DECODERS is None:
        hints = get_type_hints(ScrapeResult)
        _FIELD_DECODERS = {
            field.name: _decoder_for(_unwrap_optional(hints[field.name]))
            for field in fields(ScrapeResult)
        }
    return _FIELD_DECODERS


def _unwrap_optional(annotation: Any) -> Any:
    """``X | None`` -> ``X``; anything else unchanged."""
    if get_origin(annotation) is None:
        return annotation
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    return args[0] if len(args) == 1 else annotation


def _decoder_for(annotation: Any) -> Any:
    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return annotation
        if annotation is uuid.UUID:
            return uuid.UUID
        if annotation is datetime:
            return datetime.fromisoformat
        if annotation is Decimal:
            return Decimal
    return lambda value: value
