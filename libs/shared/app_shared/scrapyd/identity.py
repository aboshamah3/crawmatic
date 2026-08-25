"""Canonical dispatch identity — what makes two POSTs "the same dispatch".

EPA B1 (READY-002, fixes P0.4A). Pure: stdlib only, no Redis, no
SQLAlchemy, no ``requests``. The durable side lives in
:mod:`app_shared.jobs.dispatch_intents`; the Redis/HTTP side in
:mod:`app_shared.scrapyd.client`.

Why the positional key had to go
--------------------------------
The idempotency key used to be ``dispatched:{scrape_job_id}:{batch_index}``.
``batch_index`` is the enumerated position of a chunk inside a plan that
the dispatcher **re-derives** on every delivery
(:func:`app_shared.jobs.batching.plan_batches`), so its meaning changes
whenever the input set changes. Concretely, once part of a job finished,
the next plan's ``batch_index=0`` covered a *different, smaller* set of
matches than the ``batch_index=0`` whose jobid was already committed
under that key — and the client dutifully answered the new work with the
old jobid and never POSTed it. Targets wedged; only the guard's TTL ever
un-wedged them, at which point the same key silently changed meaning
again.

What replaces it
----------------
A :class:`DispatchIdentity` names the *work*, not its position:

``scrape_job_id | planning_generation | strategy_method | domain | mode |
node_class | work_digest``

* **work_digest** — ``sha256`` over the **sorted** match ids, first 16
  hex chars. Order-insensitive (the planner's chunk order is not a
  semantic difference) but subset-sensitive (a replanned fallback subset
  can never alias the full batch it came from).
* **planning_generation** — the durable counter on
  ``scrape_jobs.planning_generation``. It advances only when the planner
  **commits** a state transition (a strategy-chain advance/fallback, or a
  stall re-plan) in the same transaction as the plan itself. It is
  *never* minted per task run, which is exactly what makes a Celery
  replay reuse it verbatim and produce one POST rather than two.
* **node_class** — the class of node the work is bound for
  (``{project}:{spider}``), deliberately **not** the selected node URL:
  adding a node to a pool re-maps :func:`app_shared.jobs.nodes.select_node`
  for some domains, and that must not mint a new identity for work
  already POSTed.
* **batch_index** appears nowhere. It survives only as a spider argument.

``key`` keeps the ``dispatched:{scrape_job_id}:...`` prefix so
:func:`app_shared.jobs.cancellation._delete_dispatch_guards`'s
``dispatched:{job}:*`` scan still finds every guard for a job.

The guard value
---------------
A committed guard is JSON, not a bare jobid::

    {"jobid": ..., "identity_payload": "<canonical_payload>",
     "intent_id": ..., "committed_at": ...}

Carrying the payload inside the value is what makes
:func:`get_committed_dispatch` able to *refuse*: a value that does not
describe the identity being asked about is not an answer, it is a
collision or a legacy key, and answering with its jobid would be the
P0.4A wedge all over again. A bare pre-B1 string decodes to ``None``
(unknown), never to a false confirmation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "PENDING_SENTINEL",
    "CommittedDispatch",
    "DispatchIdentity",
    "DispatchIntentAuthority",
    "build_dispatch_identity",
    "compute_work_digest",
    "decode_guard_value",
    "encode_guard_value",
    "get_committed_dispatch",
]

#: Marks a claimed-but-not-yet-scheduled slot in Redis. Distinct from any
#: committed guard value (which is JSON) so "in flight" and "done" can
#: never be confused. Unchanged from pre-B1 on purpose: a sentinel
#: written by an older worker is still recognised by a newer one.
PENDING_SENTINEL = "__dispatch_pending__"

#: ``work_digest`` width. 16 hex chars = 64 bits over a sorted match-id
#: list; the identity's own digest below is 128 bits.
_WORK_DIGEST_HEX_CHARS = 16
#: ``key`` suffix width — 32 hex chars = 128 bits of the identity digest.
_IDENTITY_DIGEST_HEX_CHARS = 32


def compute_work_digest(match_ids: Iterable[Any]) -> str:
    """``sha256`` over the **sorted** match ids, first 16 hex chars.

    Sorting is the whole point: the planner's chunk order is an artifact
    of how targets came back from Postgres, not a property of the work,
    so ``[a, b]`` and ``[b, a]`` must be one dispatch. Ids are stringified
    first so a ``uuid.UUID`` and its text spelling hash identically —
    ``plan_batches`` yields ``UUID`` objects while a replayed Celery task
    payload carries strings, and those two must not disagree about what
    the work is.
    """
    normalized = sorted(str(match_id) for match_id in match_ids)
    joined = ",".join(normalized)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_WORK_DIGEST_HEX_CHARS]


@dataclass(frozen=True)
class DispatchIdentity:
    """The canonical name of one dispatch. Frozen — an identity is evidence.

    Every field is part of the name; none of them is positional. Two
    dispatches with equal fields ARE the same dispatch and must never
    both reach Scrapyd; two dispatches differing in any field are
    different work and must never suppress one another.
    """

    scrape_job_id: str
    #: DURABLE: persisted on the job's strategy-chain state when the
    #: planner advances; reused verbatim on Celery replay. A new
    #: generation requires a durable, explicit state transition
    #: (replan/fallback committed to DB) — never a task retry.
    planning_generation: int
    strategy_method: str
    domain: str
    mode: str  # HTTP | BROWSER
    node_class: str
    work_digest: str  # sha256 over sorted match_ids, first 16 hex chars

    @property
    def canonical_payload(self) -> str:
        """The exact string the identity digest — and the guard — is over."""
        return "|".join(
            [
                self.scrape_job_id,
                str(self.planning_generation),
                self.strategy_method,
                self.domain,
                self.mode,
                self.node_class,
                self.work_digest,
            ]
        )

    @property
    def key(self) -> str:
        """The Redis idempotency key. Job-prefixed so a job's guards stay scannable."""
        digest = hashlib.sha256(self.canonical_payload.encode()).hexdigest()[
            :_IDENTITY_DIGEST_HEX_CHARS
        ]
        return f"dispatched:{self.scrape_job_id}:{digest}"


def build_dispatch_identity(
    *,
    scrape_job_id: Any,
    planning_generation: int,
    strategy_method: Any,
    domain: str,
    mode: Any,
    node_class: str,
    match_ids: Iterable[Any],
) -> DispatchIdentity:
    """Build a :class:`DispatchIdentity`, normalizing every field to text.

    ``mode`` accepts a :class:`~app_shared.enums.ScrapeProfileMode` or a
    plain string; enums are reduced to their ``value`` so an identity
    built from ORM state and one rebuilt from a Celery payload agree.
    """
    return DispatchIdentity(
        scrape_job_id=str(scrape_job_id),
        planning_generation=int(planning_generation),
        strategy_method=str(getattr(strategy_method, "value", strategy_method)),
        domain=str(domain),
        mode=str(getattr(mode, "value", mode)),
        node_class=str(node_class),
        work_digest=compute_work_digest(match_ids),
    )


@dataclass(frozen=True)
class CommittedDispatch:
    """A dispatch that provably reached Scrapyd, and the identity it was for.

    ``source`` records *which* authority answered — ``"redis"`` for the
    fast guard, ``"dispatch_intents"`` for the durable row. Kept because
    the two can disagree only in one direction (Redis expires, the row
    does not), and an operator reading a log line needs to know which one
    stopped a re-POST.
    """

    jobid: str
    identity_payload: str
    intent_id: str | None
    committed_at: str | None
    source: str


@runtime_checkable
class DispatchIntentAuthority(Protocol):
    """The durable side of dispatch idempotency (``dispatch_intents``).

    Implemented by :class:`app_shared.jobs.dispatch_intents.DispatchIntentStore`.
    Declared here as a Protocol so
    :class:`~app_shared.scrapyd.client.ScrapydDispatchClient` can consult
    it without importing SQLAlchemy — the client stays a plain
    ``requests`` + ``redis`` object, and a caller with no database (the
    thin SPEC-07 task) simply passes ``None``.
    """

    def reconcile(self, identity: DispatchIdentity) -> CommittedDispatch | None:
        """The committed dispatch for ``identity``, or ``None``.

        Raises
        :class:`~app_shared.scrapyd.errors.StaleCancellationGenerationError`
        when the intent was authorized under a superseded cancellation
        generation.
        """
        ...

    def record_post(self, identity: DispatchIdentity) -> str: ...

    def confirm(self, identity: DispatchIdentity, scrapyd_job_id: str) -> None: ...

    def fail(self, identity: DispatchIdentity, error: str) -> None: ...


def encode_guard_value(
    identity: DispatchIdentity,
    *,
    jobid: str,
    intent_id: str | None,
    committed_at: str | None,
) -> str:
    """Render the committed guard value (see the module docstring)."""
    return json.dumps(
        {
            "jobid": str(jobid),
            "identity_payload": identity.canonical_payload,
            "intent_id": None if intent_id is None else str(intent_id),
            "committed_at": committed_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_guard_value(raw: str | None) -> CommittedDispatch | None:
    """Parse a stored guard value; ``None`` when it is not a commitment.

    ``None`` for: an absent key, the pending sentinel, a pre-B1 bare
    jobid string, and anything that does not parse. All of those mean
    "this key does not prove a POST happened" — which is the only safe
    reading, since the alternative is answering new work with an old
    jobid.
    """
    if raw is None or raw == PENDING_SENTINEL:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    jobid = payload.get("jobid")
    identity_payload = payload.get("identity_payload")
    if not jobid or not identity_payload:
        return None
    return CommittedDispatch(
        jobid=str(jobid),
        identity_payload=str(identity_payload),
        intent_id=payload.get("intent_id"),
        committed_at=payload.get("committed_at"),
        source="redis",
    )


def get_committed_dispatch(
    redis_or_db: Any, identity: DispatchIdentity
) -> CommittedDispatch | None:
    """The committed dispatch for ``identity`` — **only** on an exact match.

    ``redis_or_db`` is a Redis-like client, a
    :class:`DispatchIntentAuthority` (the ``dispatch_intents`` store), or
    a sequence of both to try in order. Redis is the fast path; the
    durable intent is consulted when the Redis guard is absent or has
    expired, because the guard's TTL says nothing about whether a POST
    happened — only the intent row does.

    Returns ``None`` unless the stored ``identity_payload`` equals
    ``identity.canonical_payload``. That equality check is the contract
    B2 depends on: a jobid recorded for *different* work is not an answer
    about *this* work, and treating it as one is precisely the aliasing
    bug B1 exists to remove.
    """
    if isinstance(redis_or_db, (list, tuple)):
        sources: Sequence[Any] = redis_or_db
    else:
        sources = (redis_or_db,)

    for source in sources:
        committed = _committed_from_source(source, identity)
        if committed is None:
            continue
        if committed.identity_payload != identity.canonical_payload:
            # A value under this key that describes other work. Never an
            # answer — fall through and keep looking.
            continue
        return committed
    return None


def _committed_from_source(
    source: Any, identity: DispatchIdentity
) -> CommittedDispatch | None:
    """Read one source. Duck-typed: ``reconcile`` (durable) beats ``get`` (Redis)."""
    if source is None:
        return None
    reconcile = getattr(source, "reconcile", None)
    if callable(reconcile):
        return reconcile(identity)
    getter = getattr(source, "get", None)
    if callable(getter):
        return decode_guard_value(getter(identity.key))
    return None
