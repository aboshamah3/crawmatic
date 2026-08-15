"""Transactional outbox for post-commit Celery work (audit risk H1).

Public surface::

    from app_shared.outbox import write_outbox_message   # producers
    from app_shared.outbox import drain_outbox           # dispatcher pass
    from app_shared.outbox import sweep_outbox           # reconcile/retention

See :mod:`app_shared.models.outbox` for the table's shape and the
partitioning/RLS decisions, :mod:`app_shared.outbox.writer` for the
same-transaction guarantee, :mod:`app_shared.outbox.dispatcher` for the
at-least-once publish + dead-lettering, and
:mod:`app_shared.outbox.reconciler` for backlog health and retention.
"""

from __future__ import annotations

from app_shared.outbox.dispatcher import DrainReport, drain_outbox, next_backoff_seconds
from app_shared.outbox.reconciler import SweepReport, sweep_outbox
from app_shared.outbox.writer import write_outbox_message

__all__ = [
    "DrainReport",
    "SweepReport",
    "drain_outbox",
    "next_backoff_seconds",
    "sweep_outbox",
    "write_outbox_message",
]
