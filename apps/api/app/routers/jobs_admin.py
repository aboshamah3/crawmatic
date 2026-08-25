"""Audited job cancellation endpoint (EPA A2, 2026-08-25).

``POST /v1/admin/jobs/{job_id}/cancel`` — the HTTP face of
:func:`app_shared.jobs.cancellation.cancel_and_reconcile_job`, the only
sanctioned way to close a scrape job whose targets nothing will ever
pick up again.

Why a router of its own rather than another route on ``routers/jobs.py``
or ``routers/admin.py``:

* Not ``routers/jobs.py`` — everything there is ordinary tenant traffic
  gated on ``jobs:read``/``jobs:write``/``jobs:run``. Cancellation is an
  administrative, irreversible terminalization, and putting it in that
  file invites the next reader to gate it with the same scope as its
  neighbours.
* Not ``routers/admin.py`` — that surface is the SaaS **control plane**:
  cross-workspace by construction, guarded by a static service token,
  running on ``get_auth_session()`` (BYPASSRLS) with every statement
  annotated ``# noqa: workspace-scope``. Cancellation is the opposite
  posture. It acts on exactly one workspace's job, and it must run on
  the tenant auth seam so RLS is the second isolation layer behind the
  explicit ``workspace_id`` predicates in ``cancellation.py``. Handing a
  destructive tenant operation to a cross-workspace BYPASSRLS session
  would delete the only structural protection against cancelling the
  wrong customer's job.

So this is a tenant-seam router that happens to live under the
``/v1/admin`` path prefix: the path says "operator surface", the auth
says "one workspace, scope-gated". The two ``/v1/admin`` routers never
collide — no path is served by both, and neither one's auth dependency
can reach the other's routes.

Authorization follows the scoped connector-keys idiom in
``routers/admin.py`` (audit P0.3): a narrow, separately grantable
capability rather than a reuse of a broad one. The gate is
:data:`~app_shared.security.scopes.Scope.JOBS_CANCEL` (``jobs:cancel``),
which is in **neither** ``BOOTSTRAP_SCOPES`` nor ``CONNECTOR_SCOPES`` —
a store connector living in a WordPress install can start jobs and can
never close them.

Audit trail. Three durable records, none of which depends on a log
scrape surviving:

1. every cancelled ``scrape_job_targets`` row carries
   ``cancelled_by`` / ``cancelled_reason`` / ``cancelled_at``;
2. the ``scrape.job.cancelled`` webhook event carries the actor and the
   reason in its payload;
3. a structured ``admin_job_cancel`` log line, in the shape
   ``routers/admin.py``'s reconciliation endpoint already uses.

The **actor is the authenticated principal**, never a request field. A
caller-suppliable actor is a signature the key holder can forge, which
would make the whole audit trail decorative.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app_shared.jobs.cancellation import cancel_and_reconcile_job
from app_shared.security.scopes import Scope

from app.deps import Principal, require_scopes
from app.schemas.jobs import JobCancelRequest, JobCancelResponse

router = APIRouter(prefix="/v1/admin/jobs", tags=["admin", "jobs"])
logger = logging.getLogger(__name__)


def principal_actor(principal: Principal) -> str:
    """The audit identity of ``principal``: ``"<kind>:<id>"``.

    Qualified by kind because a ``users`` id and an ``api_keys`` id are
    drawn from different tables — a bare UUID in an audit column is
    unresolvable six months later, and two different rows could in
    principle collide. ``user:<uuid>`` / ``api_key:<uuid>`` always
    resolves to exactly one row in exactly one table.
    """
    return f"{principal.kind}:{principal.id}"


@router.post("/{job_id}/cancel", response_model=JobCancelResponse)
def cancel_job(
    job_id: uuid.UUID,
    payload: JobCancelRequest,
    principal_ctx: tuple = Depends(require_scopes(str(Scope.JOBS_CANCEL))),
) -> JobCancelResponse:
    """Terminalize ``job_id``'s remaining targets as ``CANCELLED``.

    Never invents an outcome: a ``COMPLETED``/``FAILED``/``SKIPPED``
    target is left exactly as it is, and the ones that move become
    ``CANCELLED`` (a status that means "a human closed this"), never
    ``COMPLETED``.

    Idempotent. Calling it again on an already-cancelled job cancels
    nothing, answers ``idempotent_replay=true``, emits no second event,
    and still re-runs the out-of-band cleanup — which is what makes a
    crash part-way through recoverable by simply calling it again.

    Returns 200 (not 202): the fence — job status ``CANCELLED`` plus the
    bumped ``cancellation_generation`` — is committed before this
    responds, so a 200 means the job is *durably* closed. The Scrapyd
    cancel, the Redis guard deletion and the reservation release are
    best-effort cleanup after that commit; none of them can change the
    answer, so none of them is worth making the caller wait on a 202 and
    poll for.

    That commit happens inside ``cancel_and_reconcile_job``, not in
    ``deps.get_current_principal`` — the dependency commits only after
    this handler returns, which is *after* the out-of-band cleanup would
    have run (EPA Phase A review F-1). The request transaction is
    therefore already closed when this function resumes: nothing below
    the call may perform another workspace-scoped read or write, because
    ``SET LOCAL app.workspace_id`` died with that transaction. Building
    the response out of the returned report (a plain dataclass) rather
    than out of ORM rows is what keeps that true.
    """
    session, principal = principal_ctx
    assert isinstance(principal, Principal)

    actor = principal_actor(principal)
    try:
        report = cancel_and_reconcile_job(
            session,
            scrape_job_id=str(job_id),
            actor=actor,
            reason=payload.reason,
        )
    except LookupError as exc:
        # The job id does not resolve *in this principal's workspace* —
        # which covers both "no such job" and "someone else's job", and
        # deliberately answers the same way for both. Distinguishing them
        # would turn this endpoint into a cross-tenant existence oracle.
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Job not found."}},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_CANCELLATION", "message": str(exc)}},
        ) from exc

    response = JobCancelResponse(
        job_id=uuid.UUID(report.job_id),
        targets_cancelled=report.targets_cancelled,
        targets_already_terminal=report.targets_already_terminal,
        outbox_message_id=(
            uuid.UUID(report.outbox_message_id) if report.outbox_message_id else None
        ),
        idempotent_replay=report.idempotent_replay,
        actor=actor,
    )
    logger.info(
        "admin_job_cancel actor=%s workspace_id=%s reason=%s report=%s",
        actor,
        principal.workspace_id,
        payload.reason,
        response.model_dump(mode="json"),
    )
    return response
