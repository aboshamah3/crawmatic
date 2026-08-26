"""Network-ledger boundary recorder (EPA C4, READY-005).

Public surface: :mod:`app_shared.netledger.recorder`. C1 owns the tables,
C3 owns the authorization gate, and this package owns the single writer
that turns "a socket opened" into a row. See the recorder module
docstring for the open/close contract and its deliberately asymmetric
failure semantics.
"""

from __future__ import annotations

from app_shared.netledger.buffer import (
    KIND_CHILD_OPERATION,
    KIND_CLOSE_RECOVERY,
    BufferedEvent,
    DurableEventBuffer,
)
from app_shared.netledger.recorder import (
    CloseReceipt,
    FlushReport,
    LedgerCloseDeferred,
    LedgerError,
    LedgerOpenError,
    NetLedgerRecorder,
    OperationIntent,
    OperationOutcome,
    canonical_url_hash,
)

__all__ = [
    "KIND_CHILD_OPERATION",
    "KIND_CLOSE_RECOVERY",
    "BufferedEvent",
    "CloseReceipt",
    "DurableEventBuffer",
    "FlushReport",
    "LedgerCloseDeferred",
    "LedgerError",
    "LedgerOpenError",
    "NetLedgerRecorder",
    "OperationIntent",
    "OperationOutcome",
    "canonical_url_hash",
]
