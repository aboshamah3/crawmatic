"""Provider usage import and reconciliation — the CONSUMER side of C1's
ledger (EPA C5, READY-005 part 2, fixes the 147-vs-70 canary gap).

C4 (:mod:`app_shared.netledger.recorder`) populates C1's physical ledger
from the fleet's OWN transport observations. Neither C1 nor C4 knows what
the provider itself billed. The 2026-08-24 canary evidence bundle
(``/srv/crawmatic/evidence/canary-2026-08-24/``) is exactly this problem
made concrete: DataImpulse reports **147 billable requests** for the
canary window; the engine's own request accounting names **68-70**
proxy-attributed rows for the same job. Nobody can say whether that gap
is real overbilling, redirect-chain/sub-resource under-counting (the
known, explained C4 fact — see below), or something else, without the
provider's own record of the same window next to the fleet's. This
module is that comparison, plus the append-only cost write it produces.

Two functions, in order
------------------------
1. :func:`import_provider_usage` persists a parsed provider export
   (:class:`ProviderUsageSource`) to ``provider_usage_records`` — see
   ``app_shared.models.provider_usage`` — as immutable evidence, one row
   per provider-reported line, never merged or summarized away. Content-
   addressed (``sha256:`` over the exact export bytes) so a re-import of
   the SAME file is a no-op and a genuinely different file (a
   correction, a wider pull) always lands as new rows.
2. :func:`reconcile_window` reads those rows back, pairs them against
   :class:`~app_shared.models.network_operations.NetworkOperation` rows
   in the same window (+ an overlap grace), and returns a
   :class:`ReconciliationReport`. Where the two sides agree within
   tolerance, it APPENDS ``network_operation_settlements`` rows (C1's
   contract: reconciled cost is written, never a mutation of the
   operation) — never for a mismatch, which is reported as
   :class:`UnexplainedUsage` instead of guessed away.

Reconciliation policy — stated up front, per the run's requirement
--------------------------------------------------------------------
* **Window boundaries are UTC**, always (:class:`ProviderUsageSource`
  rejects a naive datetime). Provider and app clocks are never assumed
  to agree to the second, so operations are matched against
  ``[window_start - overlap_grace, window_end + overlap_grace]``
  (:data:`DEFAULT_OVERLAP_GRACE`, 15 minutes — chosen to absorb ordinary
  clock/export-boundary skew without pulling in an adjacent window's
  traffic; a provider whose export boundaries are known to skew further
  should pass a wider grace explicitly).
* **Matching is host/window-level, not per-request.** DataImpulse's
  export (and most metered-proxy exports) names a target host, a time,
  and bytes — not a correlation id back to a specific fetch. A
  per-request join is therefore structurally unavailable most of the
  time; this module groups both sides by ``(provider, provider_account,
  target_host)`` inside the window and reconciles at that granularity,
  which is the honest ceiling on precision the data supports. Where a
  provider DOES support a dedicated sub-account or a session/correlation
  tag (``sub_user``/``pool_identifier`` on :class:`ProviderUsageRow`),
  narrower ``provider_account`` scoping already shrinks the ambiguity —
  use it.
* **Late-arriving provider rows append a new settlement version.** A
  second :func:`import_provider_usage` call for an overlapping window
  (a wider export, a correction) followed by a second
  :func:`reconcile_window` call NEVER edits an existing settlement row —
  C1's trigger would reject it outright — it inserts
  ``settlement_version = previous_max + 1``. See "Idempotent
  reconciliation, not idempotent reruns" below for how a harmless RE-run
  of the SAME window is told apart from a genuine late arrival.
* **Rate changes are keyed by `rate_effective_date` by construction, not
  by re-derivation.** This module never invents a $/byte rate. When the
  provider export carries an independent window-level cost
  (:attr:`ProviderUsageSource.total_cost_minor_units`), that total is
  apportioned across matched operations by transport-observed bytes
  (``method=PRO_RATA_BYTES``) — a pure split of a KNOWN total, not a
  rate computation. When no independent cost is available (the common
  case — a per-request DataImpulse export does not itself carry a $
  figure), the reconciled cost is the operation's OWN
  ``estimated_cost_minor_units``, which C3/C4 already priced using the
  rate in effect on ITS ``rate_effective_date`` at close time. Either
  way, a rate that changed mid-window is already reflected in each
  operation's own prior estimate; this module apportions or confirms,
  it does not re-price.
* **Single currency per provider account; timestamped FX conversion is
  a documented open item, not a silent guess.** :func:`reconcile_window`
  raises :class:`ReconciliationPolicyError` if the matched operations (or
  the window) name more than one currency — see :func:`_single_currency`.
  No conversion table is wired; a genuinely multi-currency provider
  account needs one built, not assumed here.
* **Provider invoice corrections append `method=CORRECTION` versions.**
  Any settlement written for an operation that ALREADY has one (from an
  earlier reconciliation run, of this window or an earlier one) is
  always ``method=CORRECTION``, regardless of whether the underlying
  computation this time was an exact or pro-rata split — see
  ``app_shared.models.network_operations.SettlementMethod``'s own
  docstring for why the method label describes the *fact*, not the
  arithmetic that produced it.

Idempotent reconciliation, not idempotent reruns-that-do-nothing
-------------------------------------------------------------------
A nightly beat task that reconciles "yesterday" will, in the ordinary
case, run more than once against the exact same imported window (a
retry after a transient failure, a re-run of the same cadence tick).
That must NOT append a new settlement version each time — a version
bump is supposed to mean "new information arrived", and re-deriving the
identical number from the identical inputs is not new information.
Each settlement row's ``provider_usage_record_id`` therefore doubles as
a provenance marker (either the one ``provider_usage_records.id`` it was
derived from 1:1, or ``"window:<window_id>"`` for a pro-rata group
split); if the LATEST existing settlement for an operation already
carries the exact provenance this run would write, the run skips it
(counted in :attr:`ReconciliationReport.settlements_skipped_idempotent`)
instead of appending a redundant version. A genuinely different window
(different ``window_id``, e.g. a late/corrected import) always has a
different provenance string and therefore always appends.

Two explained-not-unexplained facts, honestly classed
---------------------------------------------------------
1. **Redirect chains.** C4's boundary middleware (the scraping
   runtime's ``netledger_middleware`` downloader middleware) records a
   redirect chain as
   ONE ``network_operations`` row with SUMMED transport bytes — a
   deliberate, test-pinned accounting choice. A metered proxy provider,
   by contrast, typically bills every hop as its own request. That means
   ``provider_requests`` legitimately EXCEEDS ``app_requests`` for a
   window with redirect traffic even when every byte reconciles exactly.
   This module does not try to reverse-engineer a per-hop count C1's
   schema does not carry (the middleware's own docstring names this an
   open schema question, not something to guess at downstream); instead,
   whenever a host group's BYTES already reconcile within tolerance, any
   remaining request-COUNT delta is recorded in
   ``ReconciliationReport.explained_variance["redirect_or_multi_hop_
   request_count_delta"]`` rather than being flagged unexplained.
2. **Buffered browser sub-resources.** C4's recorder writes each
   sub-resource as its own child ``network_operations`` row
   (``parent_operation_id`` set) once flushed. A parent+children group
   whose SUMMED bytes reconcile against the provider's per-host total is
   therefore a legitimate multi-row match under the same host-level
   grouping this module already does — no special case needed, which is
   exactly what the "synthetic matched window" step-1 test proves (a
   parent navigation + its buffered children, once flushed, reconcile a
   window that a pre-C4 world — no operations recorded at all, the real
   2026-08-24 canary's actual state — could never explain).

Seam
----
Every write and read here goes through the sanctioned **BYPASSRLS
system seam** (:func:`app_shared.database.get_system_session`), the same
choice C1's allocation writer and C4's sweeper make and for the
identical reason: ``provider_usage_records`` and the ledger tables this
module reads/writes have NO ``workspace_id`` at all (fleet facts), and
FORCE RLS on a tenant connection would see nothing or, worse, a
spuriously partial view of a cross-tenant/cross-provider aggregate.

Framework-free: SQLAlchemy + stdlib + ``app_shared`` only (Constitution
V, ``tests/unit/test_import_boundaries.py``'s posture) — no celery, no
scrapy, no fastapi.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app_shared.ids import new_uuid7
from app_shared.models.network_operations import (
    NetworkOperation,
    NetworkOperationSettlement,
    SettlementMethod,
    allocate_cost_largest_remainder,
)
from app_shared.models.provider_usage import ProviderUsageGranularity, ProviderUsageRecord

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BYTE_VARIANCE_THRESHOLD_PCT",
    "DEFAULT_OVERLAP_GRACE",
    "ProviderUsageRow",
    "ProviderUsageSource",
    "ProviderUsageWindow",
    "ReconciliationPolicyError",
    "ReconciliationReport",
    "UnexplainedUsage",
    "import_provider_usage",
    "reconcile_window",
    "windows_pending_reconciliation",
]

#: The step-4 target: a window whose transport-observed bytes and
#: provider-billed bytes agree within this percentage passes.
DEFAULT_BYTE_VARIANCE_THRESHOLD_PCT = 2.0

#: Absorbs ordinary clock/export-boundary skew between the provider's
#: dashboard and this fleet's own clocks without pulling an adjacent
#: window's traffic into a reconciliation it does not belong to.
DEFAULT_OVERLAP_GRACE = timedelta(minutes=15)

SessionScope = Callable[[], Any]


class ReconciliationPolicyError(RuntimeError):
    """A stated reconciliation policy was violated (e.g. mixed currency).

    Raised rather than silently guessed through — see the module
    docstring's "single currency per provider account" policy.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderUsageRow:
    """One line of a provider's usage export, parsed but not yet persisted.

    Field names follow the DataImpulse export runbook's documented
    columns (``/srv/crawmatic/evidence/canary-2026-08-24/
    OWNER_RUNBOOK_dataimpulse_export.md`` § "What to export — exactly"),
    kept provider-agnostic (nothing DataImpulse-specific lives here —
    that belongs to ``scripts/import_dataimpulse_usage.py``'s parser) so
    a second metered provider needs only a second parser, never a second
    reconciliation engine.
    """

    #: The row's own timestamp. ``None`` on a daily-granularity export
    #: that reports no finer time than the whole window.
    occurred_at: datetime | None
    target_host: str | None
    bytes_up: int | None
    bytes_down: int | None
    total_bytes: int
    #: How many billed requests THIS ROW represents. 1 on a per-request
    #: row; >1 on an hourly/daily aggregate row.
    request_count: int = 1
    http_status: int | None = None
    success: bool | None = None
    #: Credential-ADJACENT identifiers (never a password/token — see
    #: ``app_shared.models.provider_usage``'s warning to the importer).
    sub_user: str | None = None
    pool_identifier: str | None = None
    country: str | None = None
    billing_line_item: str | None = None
    #: The row exactly as the export presented it, before this
    #: dataclass's own field mapping — preserved end to end into
    #: ``provider_usage_records.raw_row``.
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.total_bytes, bool) or not isinstance(self.total_bytes, int):
            raise TypeError(
                f"total_bytes must be an int of bytes, got {type(self.total_bytes)!r} "
                "— never a float"
            )
        if self.total_bytes < 0:
            raise ValueError(f"total_bytes must be non-negative: {self.total_bytes!r}")
        if isinstance(self.request_count, bool) or not isinstance(self.request_count, int):
            raise TypeError(
                f"request_count must be an int, got {type(self.request_count)!r}"
            )
        if self.request_count < 0:
            raise ValueError(f"request_count must be non-negative: {self.request_count!r}")
        if self.occurred_at is not None and self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class ProviderUsageSource:
    """One parsed provider export, ready for :func:`import_provider_usage`.

    ``raw_bytes`` MUST be the exact bytes the importer read from the
    export file (or an equivalent canonical encoding for a non-file
    source) — ``source_hash`` is computed over exactly these bytes, so
    re-importing the identical export is idempotent at the row level and
    a genuinely different export always gets a new hash.

    ``total_cost_minor_units``/``currency`` are OPTIONAL: most metered-
    proxy per-request exports (DataImpulse's included, per the runbook)
    do not themselves carry a dollar figure — that lives on a separate
    invoice. When present, they name an INDEPENDENT window-level cost
    fact this module apportions by bytes rather than re-derives; see the
    module docstring's "rate changes" policy.
    """

    provider: str
    window_start: datetime
    window_end: datetime
    rows: Sequence[ProviderUsageRow]
    source_ref: str
    raw_bytes: bytes
    granularity: ProviderUsageGranularity = ProviderUsageGranularity.PER_REQUEST
    provider_account: str | None = None
    total_cost_minor_units: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError(
                "window_start/window_end must be timezone-aware UTC datetimes — "
                "the reconciliation policy is stated in UTC throughout"
            )
        if self.window_end < self.window_start:
            raise ValueError("window_end must not precede window_start")
        if self.total_cost_minor_units is not None:
            if (
                isinstance(self.total_cost_minor_units, bool)
                or not isinstance(self.total_cost_minor_units, int)
            ):
                raise TypeError("total_cost_minor_units must be an int, never a float")
            if self.total_cost_minor_units < 0:
                raise ValueError("total_cost_minor_units must be non-negative")
            if self.currency is None:
                raise ValueError("total_cost_minor_units requires a currency")


@dataclass(frozen=True)
class ProviderUsageWindow:
    """One persisted import — what :func:`import_provider_usage` returns
    and what :func:`reconcile_window` consumes."""

    id: uuid.UUID
    provider: str
    provider_account: str | None
    window_start: datetime
    window_end: datetime
    source_hash: str
    source_ref: str
    row_count: int
    total_requests: int
    total_bytes: int
    total_cost_minor_units: int | None
    currency: str | None
    imported_at: datetime
    #: True when this call found an existing import with the same
    #: ``source_hash`` and wrote nothing new (idempotent re-import).
    already_imported: bool = False


@dataclass(frozen=True)
class UnexplainedUsage:
    """One thing this reconciliation could NOT explain against C1's ledger.

    ``kind`` is one of:

    * ``PROVIDER_ROWS_WITHOUT_OPERATION`` — the provider billed activity
      for ``target_host`` in this window and no ``network_operations``
      row exists to account for it at all. This is the canary's own
      shape: a window with NO ledger (pre-C4) or a genuine dispatch gap.
    * ``OPERATION_WITHOUT_PROVIDER_EVIDENCE`` — the fleet recorded a
      physical fetch for ``target_host`` that the provider's export never
      mentions. Not a cost risk (nothing to reconcile a charge against),
      but surfaced for completeness rather than silently dropped.
    * ``BYTE_VARIANCE_EXCEEDS_THRESHOLD`` — both sides have rows for
      ``target_host``, but their byte totals disagree by more than the
      threshold even after accounting for the redirect/subresource
      request-count facts (which explain a COUNT delta, never a BYTE
      one).
    """

    kind: str
    target_host: str | None
    provider_requests: int
    provider_bytes: int
    app_operation_ids: tuple[uuid.UUID, ...]
    app_bytes: int
    detail: str


@dataclass(frozen=True)
class ReconciliationReport:
    """The reconciliation verdict for one provider usage window."""

    window_id: uuid.UUID
    provider: str
    provider_account: str | None
    window_start: datetime
    window_end: datetime
    app_requests: int
    provider_requests: int
    #: TRANSPORT-OBSERVED bytes, summed across every matched
    #: ``network_operations`` row in the window — never provider-billed
    #: bytes (those are ``provider_bytes``).
    app_bytes: int
    provider_bytes: int
    byte_variance_pct: float | None
    cost_variance_pct: float | None
    #: category -> count, e.g. ``{"redirect_or_multi_hop_request_count_delta": 79}``.
    explained_variance: Mapping[str, int]
    unexplained_operations: tuple[UnexplainedUsage, ...]
    settlements_written: tuple[uuid.UUID, ...]
    settlements_skipped_idempotent: int
    #: True iff ``unexplained_operations`` is empty. A window can PASS
    #: with a nonzero ``byte_variance_pct`` (below threshold) and a
    #: nonzero ``explained_variance`` entry — that is the whole point of
    #: classing redirect/subresource deltas as explained.
    passed: bool
    generated_at: datetime


# ---------------------------------------------------------------------------
# import_provider_usage
# ---------------------------------------------------------------------------


def import_provider_usage(
    source: ProviderUsageSource,
    *,
    session_scope: SessionScope | None = None,
    now: Callable[[], datetime] | None = None,
) -> ProviderUsageWindow:
    """Persist ``source``'s rows to ``provider_usage_records`` as immutable evidence.

    Content-addressed idempotency: if ``source_hash`` already has rows,
    nothing new is written and the existing window is returned with
    ``already_imported=True`` — re-running the importer against the same
    export file is always safe. A different export (even one describing
    an overlapping or identical time window) always has a different
    hash and therefore always lands as new rows: the provider's
    window/invoice is a fact, never merged away.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    moment = clock()
    source_hash = "sha256:" + hashlib.sha256(source.raw_bytes).hexdigest()
    total_requests, total_bytes = _aggregate_rows(source.rows)

    scope = session_scope
    if scope is None:
        from app_shared.database import get_system_session

        scope = get_system_session

    with scope() as session:
        existing = session.execute(
            select(ProviderUsageRecord.window_id)
            .where(ProviderUsageRecord.source_hash == source_hash)
            .limit(1)
        ).first()
        if existing is not None:
            window_id = existing[0]
            logger.info(
                "netledger.reconcile.import_already_present provider=%s "
                "source_hash=%s window_id=%s",
                source.provider,
                source_hash,
                window_id,
            )
            return ProviderUsageWindow(
                id=window_id,
                provider=source.provider,
                provider_account=source.provider_account,
                window_start=source.window_start,
                window_end=source.window_end,
                source_hash=source_hash,
                source_ref=source.source_ref,
                row_count=len(source.rows),
                total_requests=total_requests,
                total_bytes=total_bytes,
                total_cost_minor_units=source.total_cost_minor_units,
                currency=source.currency,
                imported_at=moment,
                already_imported=True,
            )

        window_id = new_uuid7()
        rows_to_insert = [
            {
                "id": uuid.uuid4(),
                "window_id": window_id,
                "row_ordinal": ordinal,
                "provider": source.provider,
                "provider_account": source.provider_account,
                "window_start": source.window_start,
                "window_end": source.window_end,
                "granularity": source.granularity,
                "occurred_at": row.occurred_at,
                "target_host": row.target_host,
                "bytes_up": row.bytes_up,
                "bytes_down": row.bytes_down,
                "total_bytes": row.total_bytes,
                "request_count": row.request_count,
                "http_status": row.http_status,
                "success": row.success,
                "sub_user": row.sub_user,
                "pool_identifier": row.pool_identifier,
                "country": row.country,
                "billing_line_item": row.billing_line_item,
                "raw_row": dict(row.raw),
                "source_ref": source.source_ref,
                "source_hash": source_hash,
                "imported_at": moment,
            }
            for ordinal, row in enumerate(source.rows)
        ]
        if rows_to_insert:
            session.execute(insert(ProviderUsageRecord), rows_to_insert)
        session.commit()

    logger.info(
        "netledger.reconcile.import_persisted provider=%s provider_account=%s "
        "window_id=%s rows=%d total_requests=%d total_bytes=%d source_hash=%s",
        source.provider,
        source.provider_account,
        window_id,
        len(rows_to_insert),
        total_requests,
        total_bytes,
        source_hash,
    )
    return ProviderUsageWindow(
        id=window_id,
        provider=source.provider,
        provider_account=source.provider_account,
        window_start=source.window_start,
        window_end=source.window_end,
        source_hash=source_hash,
        source_ref=source.source_ref,
        row_count=len(rows_to_insert),
        total_requests=total_requests,
        total_bytes=total_bytes,
        total_cost_minor_units=source.total_cost_minor_units,
        currency=source.currency,
        imported_at=moment,
        already_imported=False,
    )


def _aggregate_rows(rows: Sequence[ProviderUsageRow]) -> tuple[int, int]:
    total_requests = sum(row.request_count for row in rows)
    total_bytes = sum(row.total_bytes for row in rows)
    return total_requests, total_bytes


# ---------------------------------------------------------------------------
# reconcile_window
# ---------------------------------------------------------------------------


def reconcile_window(
    window: ProviderUsageWindow,
    *,
    session_scope: SessionScope | None = None,
    now: Callable[[], datetime] | None = None,
    byte_variance_threshold_pct: float = DEFAULT_BYTE_VARIANCE_THRESHOLD_PCT,
    overlap_grace: timedelta = DEFAULT_OVERLAP_GRACE,
) -> ReconciliationReport:
    """Compare ``window`` against C1's ledger and append settlements where matched.

    Reads ``provider_usage_records`` for ``window.id`` and
    ``network_operations`` closed within
    ``[window.window_start - overlap_grace, window.window_end +
    overlap_grace]`` for ``window.provider`` (+ ``provider_account`` when
    given), groups both by ``target_host``/``domain``, and for every host
    where both sides have evidence and bytes reconcile within
    ``byte_variance_threshold_pct``, appends
    ``network_operation_settlements`` rows. See the module docstring for
    the full policy this implements.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    moment = clock()

    scope = session_scope
    if scope is None:
        from app_shared.database import get_system_session

        scope = get_system_session

    with scope() as session:
        provider_rows = list(
            session.execute(
                select(ProviderUsageRecord)
                .where(ProviderUsageRecord.window_id == window.id)
                .order_by(ProviderUsageRecord.row_ordinal)
            )
            .scalars()
            .all()
        )

        lower = window.window_start - overlap_grace
        upper = window.window_end + overlap_grace
        op_query = select(NetworkOperation).where(
            NetworkOperation.provider == window.provider,
            NetworkOperation.closed_at.is_not(None),
            NetworkOperation.closed_at >= lower,
            NetworkOperation.closed_at <= upper,
        )
        if window.provider_account is not None:
            op_query = op_query.where(
                NetworkOperation.provider_account == window.provider_account
            )
        operations = list(session.execute(op_query).scalars().all())

        report, settlement_rows = _build_report_and_settlements(
            session=session,
            window=window,
            provider_rows=provider_rows,
            operations=operations,
            byte_variance_threshold_pct=byte_variance_threshold_pct,
            moment=moment,
        )

        for row in settlement_rows:
            session.execute(insert(NetworkOperationSettlement).values(**row))
        session.commit()

    logger.info(
        "netledger.reconcile.report window_id=%s provider=%s passed=%s "
        "app_requests=%d provider_requests=%d app_bytes=%d provider_bytes=%d "
        "byte_variance_pct=%s unexplained=%d settlements_written=%d "
        "settlements_skipped_idempotent=%d",
        window.id,
        window.provider,
        report.passed,
        report.app_requests,
        report.provider_requests,
        report.app_bytes,
        report.provider_bytes,
        report.byte_variance_pct,
        len(report.unexplained_operations),
        len(report.settlements_written),
        report.settlements_skipped_idempotent,
    )
    return report


def _build_report_and_settlements(
    *,
    session: Session,
    window: ProviderUsageWindow,
    provider_rows: Sequence[ProviderUsageRecord],
    operations: Sequence[NetworkOperation],
    byte_variance_threshold_pct: float,
    moment: datetime,
) -> tuple[ReconciliationReport, list[dict[str, Any]]]:
    provider_by_host: dict[str | None, list[ProviderUsageRecord]] = defaultdict(list)
    for row in provider_rows:
        provider_by_host[row.target_host].append(row)
    ops_by_host: dict[str | None, list[NetworkOperation]] = defaultdict(list)
    for op in operations:
        ops_by_host[op.domain].append(op)

    app_requests = len(operations)
    app_bytes = sum(_operation_bytes(op) for op in operations)
    provider_requests = sum(row.request_count for row in provider_rows)
    provider_bytes = sum(row.total_bytes for row in provider_rows)
    byte_variance_pct = _variance_pct(app_bytes, provider_bytes)

    explained: dict[str, int] = {}
    unexplained: list[UnexplainedUsage] = []
    matched_groups: list[
        tuple[str | None, list[NetworkOperation], list[ProviderUsageRecord]]
    ] = []

    all_hosts = sorted(set(provider_by_host) | set(ops_by_host), key=lambda h: h or "")
    for host in all_hosts:
        p_rows = provider_by_host.get(host, [])
        ops = ops_by_host.get(host, [])
        p_bytes = sum(row.total_bytes for row in p_rows)
        p_requests = sum(row.request_count for row in p_rows)
        a_bytes = sum(_operation_bytes(op) for op in ops)
        a_requests = len(ops)

        if p_rows and not ops:
            unexplained.append(
                UnexplainedUsage(
                    kind="PROVIDER_ROWS_WITHOUT_OPERATION",
                    target_host=host,
                    provider_requests=p_requests,
                    provider_bytes=p_bytes,
                    app_operation_ids=(),
                    app_bytes=0,
                    detail=(
                        f"{p_requests} provider request(s) / {p_bytes} bytes for "
                        f"{host!r} have no matching network_operations row in "
                        "this window"
                    ),
                )
            )
            continue
        if ops and not p_rows:
            unexplained.append(
                UnexplainedUsage(
                    kind="OPERATION_WITHOUT_PROVIDER_EVIDENCE",
                    target_host=host,
                    provider_requests=0,
                    provider_bytes=0,
                    app_operation_ids=tuple(op.network_request_id for op in ops),
                    app_bytes=a_bytes,
                    detail=(
                        f"{a_requests} network_operations row(s) / {a_bytes} bytes "
                        f"for {host!r} have no provider usage evidence in this window"
                    ),
                )
            )
            continue

        group_variance = _variance_pct(a_bytes, p_bytes)
        if group_variance is not None and group_variance > byte_variance_threshold_pct:
            unexplained.append(
                UnexplainedUsage(
                    kind="BYTE_VARIANCE_EXCEEDS_THRESHOLD",
                    target_host=host,
                    provider_requests=p_requests,
                    provider_bytes=p_bytes,
                    app_operation_ids=tuple(op.network_request_id for op in ops),
                    app_bytes=a_bytes,
                    detail=(
                        f"{host!r}: app bytes {a_bytes} vs provider bytes {p_bytes} "
                        f"is {group_variance:.2f}% variance "
                        f"(threshold {byte_variance_threshold_pct:.2f}%)"
                    ),
                )
            )
            continue

        # Bytes reconcile. A request-count delta here is the honestly
        # EXPLAINED redirect-chain/subresource-billing fact, not an
        # unexplained gap — see the module docstring.
        if p_requests != a_requests:
            key = "redirect_or_multi_hop_request_count_delta"
            explained[key] = explained.get(key, 0) + abs(p_requests - a_requests)

        matched_groups.append((host, ops, p_rows))

    settlement_rows, skipped = _settlements_for_matched_groups(
        session=session, window=window, matched_groups=matched_groups, moment=moment
    )

    cost_variance_pct: float | None = None
    if window.total_cost_minor_units is not None:
        app_estimated = sum(
            op.estimated_cost_minor_units or 0 for _, ops, _ in matched_groups for op in ops
        )
        if app_estimated:
            cost_variance_pct = (
                abs(window.total_cost_minor_units - app_estimated) / app_estimated * 100.0
            )

    report = ReconciliationReport(
        window_id=window.id,
        provider=window.provider,
        provider_account=window.provider_account,
        window_start=window.window_start,
        window_end=window.window_end,
        app_requests=app_requests,
        provider_requests=provider_requests,
        app_bytes=app_bytes,
        provider_bytes=provider_bytes,
        byte_variance_pct=byte_variance_pct,
        cost_variance_pct=cost_variance_pct,
        explained_variance=explained,
        unexplained_operations=tuple(unexplained),
        settlements_written=tuple(row["id"] for row in settlement_rows),
        settlements_skipped_idempotent=skipped,
        passed=len(unexplained) == 0,
        generated_at=moment,
    )
    return report, settlement_rows


def _settlements_for_matched_groups(
    *,
    session: Session,
    window: ProviderUsageWindow,
    matched_groups: Sequence[
        tuple[str | None, Sequence[NetworkOperation], Sequence[ProviderUsageRecord]]
    ],
    moment: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """Build (unwritten) settlement rows for every RECONCILED host group.

    A single largest-remainder split of ``window.total_cost_minor_units``
    (when known) across ALL matched operations in the window, weighted by
    each operation's own transport-observed bytes, guarantees the parts
    sum exactly to the provider's stated total — the same rounding
    discipline C1's allocation split uses, applied here to settlement
    rather than allocation.

    ``provider_usage_record_id`` (Text, no FK — see this migration's own
    docstring, ``7c2b9e5a41d6_provider_usage_records.py``, for why no
    foreign key is added) is populated with the ONE
    ``provider_usage_records.id`` a 1-operation/1-provider-row group can
    name exactly (``method=EXACT``), or ``"window:<window_id>"`` for a
    genuine multi-way pro-rata split (``method=PRO_RATA_BYTES``) — a
    provider-native identifier is deliberately never invented here.
    """
    all_ops: list[NetworkOperation] = []
    provenance_by_op: dict[uuid.UUID, str] = {}
    exact_by_op: dict[uuid.UUID, bool] = {}
    for _host, ops, p_rows in matched_groups:
        one_to_one = len(ops) == 1 and len(p_rows) == 1
        for op in ops:
            all_ops.append(op)
            provenance_by_op[op.network_request_id] = (
                str(p_rows[0].id) if one_to_one else f"window:{window.id}"
            )
            exact_by_op[op.network_request_id] = one_to_one

    if not all_ops:
        return [], 0

    currency = window.currency or _single_currency(all_ops)

    if window.total_cost_minor_units is not None:
        weights = [max(_operation_bytes(op), 0) for op in all_ops]
        if sum(weights) == 0:
            weights = [1] * len(all_ops)
        amounts: list[int | None] = allocate_cost_largest_remainder(
            window.total_cost_minor_units, weights
        )
    else:
        # No independent provider-billed total: the reconciled cost is
        # the operation's OWN prior estimate (already priced at its own
        # rate_effective_date by C3/C4) — this run confirms it rather
        # than inventing a new figure. See the module docstring's "rate
        # changes" policy.
        amounts = [op.estimated_cost_minor_units for op in all_ops]

    rows: list[dict[str, Any]] = []
    skipped = 0
    for op, amount in zip(all_ops, amounts, strict=True):
        if amount is None:
            logger.warning(
                "netledger.reconcile.no_cost_signal operation_id=%s window_id=%s — "
                "no window-level cost and no prior estimated_cost_minor_units; "
                "skipping settlement (byte/request facts are still reflected in "
                "the report)",
                op.network_request_id,
                window.id,
            )
            continue

        provenance = provenance_by_op[op.network_request_id]
        existing_version, existing_provenance = _latest_settlement(
            session, op.network_request_id
        )
        if existing_version is not None and existing_provenance == provenance:
            # Same fact this window already recorded (an idempotent
            # rerun of the same import) — not new information.
            skipped += 1
            continue

        method = (
            SettlementMethod.CORRECTION
            if existing_version is not None
            else (SettlementMethod.EXACT if exact_by_op[op.network_request_id]
                  else SettlementMethod.PRO_RATA_BYTES)
        )
        rows.append(
            {
                "id": new_uuid7(),
                "operation_id": op.network_request_id,
                "settlement_version": (existing_version or 0) + 1,
                "reconciled_cost_minor_units": int(amount),
                "currency": currency,
                "provider_usage_record_id": provenance,
                "method": method,
                "created_at": moment,
            }
        )
    return rows, skipped


def _latest_settlement(
    session: Session, operation_id: uuid.UUID
) -> tuple[int | None, str | None]:
    row = session.execute(
        select(
            NetworkOperationSettlement.settlement_version,
            NetworkOperationSettlement.provider_usage_record_id,
        )
        .where(NetworkOperationSettlement.operation_id == operation_id)
        .order_by(NetworkOperationSettlement.settlement_version.desc())
        .limit(1)
    ).first()
    if row is None:
        return None, None
    return int(row[0]), row[1]


def _single_currency(ops: Sequence[NetworkOperation]) -> str:
    """Policy: one currency per provider account. See module docstring."""
    currencies = {op.currency for op in ops if op.currency is not None}
    if len(currencies) > 1:
        raise ReconciliationPolicyError(
            f"operations in this reconciliation batch disagree on currency: "
            f"{sorted(currencies)!r}. Policy requires a single currency per "
            "provider account; timestamped FX conversion for a genuinely "
            "multi-currency provider account is NOT implemented here (no "
            "conversion source is wired) — this is a documented open item, not "
            "a silent guess."
        )
    return next(iter(currencies), "USD")


def _operation_bytes(op: NetworkOperation) -> int:
    """TRANSPORT-OBSERVED bytes for one operation — never provider-billed."""
    if op.bytes_compressed is not None:
        return int(op.bytes_compressed)
    if op.bytes_decompressed is not None:
        return int(op.bytes_decompressed)
    return 0


def _variance_pct(app_value: int, provider_value: int) -> float | None:
    if provider_value == 0:
        return 0.0 if app_value == 0 else None
    return abs(app_value - provider_value) / provider_value * 100.0


# ---------------------------------------------------------------------------
# Nightly beat task support
# ---------------------------------------------------------------------------


def windows_pending_reconciliation(
    session: Session, *, target_date: date, provider: str | None = None
) -> list[ProviderUsageWindow]:
    """Reconstruct every DISTINCT imported window whose ``window_start``
    falls on ``target_date`` (UTC) — the driver query for the nightly
    per-provider/day/account beat task
    (``apps.workers.app.workers.tasks_maintenance.reconcile_provider_usage``).

    Reconstructed PURELY from ``provider_usage_records`` (the durable
    evidence), so ``total_cost_minor_units``/``currency`` come back
    ``None`` here even if the original :class:`ProviderUsageSource`
    carried a window-level cost — that figure is not currently persisted
    at the window level (no table exists to hold it; the real DataImpulse
    export is owner-pending and no $ figure has ever been available to
    persist). :func:`reconcile_window` degrades correctly in that case:
    it confirms each operation's own prior cost estimate rather than
    inventing one. Adding a durable window-cost table is a documented
    follow-up, not built here.
    """
    query = select(
        ProviderUsageRecord.window_id,
        ProviderUsageRecord.provider,
        ProviderUsageRecord.provider_account,
        ProviderUsageRecord.window_start,
        ProviderUsageRecord.window_end,
        ProviderUsageRecord.source_hash,
        ProviderUsageRecord.source_ref,
    ).where(func.date(ProviderUsageRecord.window_start) == target_date)
    if provider is not None:
        query = query.where(ProviderUsageRecord.provider == provider)
    distinct_rows = session.execute(query.distinct()).all()

    windows: list[ProviderUsageWindow] = []
    for window_id, prov, account, w_start, w_end, source_hash, source_ref in distinct_rows:
        row_count, total_requests, total_bytes, imported_at = session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(ProviderUsageRecord.request_count), 0),
                func.coalesce(func.sum(ProviderUsageRecord.total_bytes), 0),
                func.min(ProviderUsageRecord.imported_at),
            ).where(ProviderUsageRecord.window_id == window_id)
        ).one()
        windows.append(
            ProviderUsageWindow(
                id=window_id,
                provider=prov,
                provider_account=account,
                window_start=w_start,
                window_end=w_end,
                source_hash=source_hash,
                source_ref=source_ref,
                row_count=int(row_count),
                total_requests=int(total_requests),
                total_bytes=int(total_bytes),
                total_cost_minor_units=None,
                currency=None,
                imported_at=imported_at,
                already_imported=True,
            )
        )
    return windows
