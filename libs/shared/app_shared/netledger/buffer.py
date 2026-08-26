"""The scraper host's DURABLE local event queue (EPA C4, READY-005).

Why a local queue exists at all
-------------------------------
C4's boundary recorder has two obligations that cannot both be met by a
synchronous database write:

1. **Nothing is silently lost.** A close that fails *after* bytes moved
   has already spent money; dropping it makes the ledger under-count,
   and an under-counting ledger cannot be reconciled against a provider
   invoice.
2. **Accounting is never the per-asset bottleneck.** One browser
   navigation pulls dozens of sub-resources. A synchronous INSERT per
   asset would put a network round-trip to Postgres inside Chromium's
   own response event loop — the accounting would cost more than the
   thing it accounts for.

Both are satisfied by the same primitive: an append-only local queue on
the scraper host that a flusher drains in batches. A sub-resource is
*buffered* (cheap, local, durable) and becomes a child ledger row later;
a failed close is *buffered* as a recovery event and replayed by the same
sweeper.

Why SQLite in WAL mode
----------------------
An append-only text file would need its own crash-safe framing, its own
"which lines are already flushed" bookkeeping, and its own concurrent-
writer story. SQLite already has all three, ships with CPython, needs no
server, and in WAL mode lets the flusher read while the interception hook
keeps appending.

**Durability, stated precisely** (never claimed beyond what is true):
with ``journal_mode=WAL`` and ``synchronous=NORMAL`` a COMMIT is written
into the WAL file with ``write(2)`` before it returns, so every committed
event survives the **process** being killed — which is the failure this
buffer exists for (a spider process exiting mid-page, a container
receiving SIGKILL). It does NOT survive an OS crash or power loss
between commit and checkpoint; ``synchronous="FULL"`` (constructor arg)
buys that at the price of an fsync per commit, which is exactly the
per-asset cost this module exists to avoid. The default is therefore
NORMAL, and the trade-off is written here rather than left implicit.

Rows are deleted only once the flusher has confirmed the corresponding
ledger write COMMITTED, so a kill between flush and delete replays the
event rather than losing it. Replay is safe because both event kinds are
idempotent by key: a child operation carries its own pre-generated
``network_request_id`` (a unique column — a replayed insert collides and
is skipped), and a close recovery targets one already-open operation
whose ``closed_at`` the C1 trigger only lets be written once.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "KIND_CHILD_OPERATION",
    "KIND_CLOSE_RECOVERY",
    "BUFFER_PATH_ENV",
    "BufferedEvent",
    "DurableEventBuffer",
    "json_default",
]

#: A browser sub-resource observed by the interception hook, to be
#: written later as a child ``network_operations`` row carrying
#: ``parent_operation_id``.
KIND_CHILD_OPERATION = "CHILD_OPERATION"

#: A close whose database write failed after network activity. Replayed
#: by the sweeper so bytes are never silently lost.
KIND_CLOSE_RECOVERY = "CLOSE_RECOVERY"

#: Where the buffer file lives. Read from ``os.environ`` (a scraper-host
#: fact, set per node) rather than from ``Settings``: ``Settings`` is
#: loaded from ``.env``/DB and is shared fleet-wide, while this path is
#: per-host and must be writable by the spider process that owns it.
BUFFER_PATH_ENV = "NETLEDGER_BUFFER_PATH"

_DEFAULT_BUFFER_PATH = "/var/lib/crawmatic/netledger/buffer.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS netledger_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS ix_netledger_events_kind_id
    ON netledger_events (kind, id);
"""


def json_default(value: Any) -> Any:
    """JSON encoder hook for the types a ledger payload actually carries.

    ``uuid.UUID``/``datetime``/``date`` become strings; anything else
    raises, deliberately — a payload this function cannot round-trip is a
    payload the sweeper would replay wrongly, and failing at append time
    surfaces that while the event is still in the caller's hands.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"netledger buffer cannot serialize {type(value)!r}")


@dataclass(frozen=True)
class BufferedEvent:
    """One durable queue entry, as handed to the flusher."""

    row_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int


class DurableEventBuffer:
    """Append-only local queue backed by one SQLite file in WAL mode.

    Safe to share across threads: one connection guarded by a lock
    (``check_same_thread=False``). The lock is held only for the duration
    of a statement batch, so an append from a Playwright event callback
    never waits on a full flush.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        synchronous: str = "NORMAL",
    ) -> None:
        resolved = str(
            path
            if path is not None
            else os.environ.get(BUFFER_PATH_ENV) or _DEFAULT_BUFFER_PATH
        )
        self.path = resolved
        if resolved != ":memory:":
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(resolved, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA synchronous={synchronous}")
        self._conn.executescript(_SCHEMA)

    # -- writing ------------------------------------------------------------

    def append(self, kind: str, payload: dict[str, Any]) -> int:
        """Durably append one event; returns its row id."""
        return self.append_many(((kind, payload),))[0]

    def append_many(self, items: Iterable[tuple[str, dict[str, Any]]]) -> list[int]:
        """Durably append a batch of events in ONE transaction.

        Batching matters for the browser path: a page's sub-resources
        arrive in bursts, and one commit per burst is what keeps the
        buffer off the critical path.
        """
        encoded = [
            (kind, json.dumps(payload, default=json_default), _now_iso())
            for kind, payload in items
        ]
        if not encoded:
            return []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                ids: list[int] = []
                for row in encoded:
                    cursor = self._conn.execute(
                        "INSERT INTO netledger_events (kind, payload, created_at) "
                        "VALUES (?, ?, ?)",
                        row,
                    )
                    ids.append(int(cursor.lastrowid or 0))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return ids

    # -- reading / draining -------------------------------------------------

    def pending(self, limit: int = 500, *, kind: str | None = None) -> list[BufferedEvent]:
        """The oldest ``limit`` unflushed events, oldest first."""
        sql = "SELECT id, kind, payload, attempts FROM netledger_events"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY id LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        events: list[BufferedEvent] = []
        for row_id, row_kind, payload, attempts in rows:
            try:
                decoded = json.loads(payload)
            except ValueError:
                # A payload that cannot be decoded can never be replayed;
                # it is left in place with the error recorded rather than
                # deleted, so it stays visible to an operator instead of
                # disappearing quietly.
                self.defer(int(row_id), "undecodable payload")
                continue
            events.append(
                BufferedEvent(
                    row_id=int(row_id),
                    kind=str(row_kind),
                    payload=decoded,
                    attempts=int(attempts),
                )
            )
        return events

    def pending_count(self, *, kind: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM netledger_events"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def resolve(self, row_ids: Sequence[int]) -> None:
        """Delete events whose ledger write has COMMITTED.

        Called only after the commit, never before: a kill in between
        replays the event (idempotent by key — see the module docstring),
        where the reverse order would lose it.
        """
        ids = [int(row_id) for row_id in row_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    f"DELETE FROM netledger_events WHERE id IN ({placeholders})", ids
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def defer(self, row_id: int, error: str) -> None:
        """Record a failed replay attempt; the event STAYS queued."""
        with self._lock:
            self._conn.execute(
                "UPDATE netledger_events SET attempts = attempts + 1, last_error = ? "
                "WHERE id = ?",
                (error[:2000], int(row_id)),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
