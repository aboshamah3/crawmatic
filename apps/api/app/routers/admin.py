"""SaaS control-plane admin endpoints (PLAN §7.1–§7.2).

Guarded by `app.service_auth.require_service_token` (static bearer,
constant-time compare) rather than the workspace auth seam in
`app.deps` — this surface is cross-workspace by construction.

Because it is cross-workspace it runs on `get_auth_session()`
(BYPASSRLS), the same narrow boundary `deps.py`/`auth.py` already use
for pre-auth lookups. Every statement here is deliberately unscoped and
annotated `# noqa: workspace-scope`.

This router is internal-only: it must never be published in the
customer-facing API docs.

**`WorkspaceStatus` substitution**: `app_shared.enums.WorkspaceStatus`
has only `ACTIVE`/`SUSPENDED` — there is no `ARCHIVED` member. Archiving
a workspace sets it to `SUSPENDED` (the only disabled/paused member),
serialized on the response as `str(WorkspaceStatus.SUSPENDED)` ==
`"suspended"`.

**`BOOTSTRAP_SCOPES` trimmed to real `Scope` members**: the brief asked
for read/write on products, variants, product_groups, competitors,
matches, alerts, jobs, refresh_rules, webhooks, scrape_profiles, and
domain_rules. `app_shared.security.scopes.Scope` has no
`product_groups:read`/`product_groups:write` (product-group management
rides on `products:write`/`variants:write` — see `main.py`'s SPEC-04 US3
docstring paragraph) and no `alerts:write` (alerts are read-only via the
API). Both are dropped below rather than invented; granting an unknown
scope string would 422 out of `validate_scopes` anyway.
`proxy_providers:*` and `access_policies:*` are deliberately excluded
per the brief — those configure spend, not tenant data, and stay
operator-only.
"""

from __future__ import annotations

import uuid
import logging
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.database import get_auth_session
from app_shared.enums import ApiKeyStatus, WorkspaceStatus
from app_shared.jobs.reconciliation import reconcile_successful_failed_targets
from app_shared.models.competitors_matches import Competitor
from app_shared.models.identity import ApiKey, Workspace
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.profiles.repository import clear_regex_quarantine
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.api_keys import generate_api_key
from app_shared.security.scopes import validate_scopes

from app.schemas.admin import (
    AdminApiKeyCreateRequest,
    AdminApiKeyCreateResponse,
    AdminApiKeyListItem,
    AdminApiKeyListResponse,
    ApiKeyRevokeResponse,
    CompetitorApprovalRequest,
    CompetitorApprovalResponse,
    ProfileRegexUnquarantineResponse,
    ConnectorKeyCreateRequest,
    ConnectorKeyCreateResponse,
    UsageListResponse,
    UsageRow,
    TargetReconciliationRequest,
    TargetReconciliationResponse,
    WorkspaceArchiveResponse,
    WorkspaceProvisionRequest,
    WorkspaceProvisionResponse,
)
from app.service_auth import require_service_token
from app.services.admin_usage import (
    InvalidUsageCursor,
    InvalidUsageWindow,
    UsageCursor,
    UsageWindowTooLarge,
    build_usage_query,
    clamp_usage_limit,
    decode_usage_cursor,
    encode_usage_cursor,
    normalize_window,
    validate_window,
)

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_service_token)]
)
logger = logging.getLogger(__name__)

#: Tenant scopes granted to a bootstrap key. Deliberately excludes
#: `proxy_providers:*` and `access_policies:*` — those configure what we
#: are willing to spend on a fetch and stay operator-only. Also excludes
#: `product_groups:*` and `alerts:write`, neither of which exists in
#: `Scope` (see module docstring).
BOOTSTRAP_SCOPES: list[str] = [
    "products:read",
    "products:write",
    "variants:read",
    "variants:write",
    "competitors:read",
    "competitors:write",
    "matches:read",
    "matches:write",
    "alerts:read",
    "jobs:read",
    "jobs:write",
    "refresh_rules:read",
    "refresh_rules:write",
    "webhooks:read",
    "webhooks:write",
    "scrape_profiles:read",
    "scrape_profiles:write",
    "domain_rules:read",
    "domain_rules:write",
]

#: What a store connector actually does (audit P0.3): read catalog and price
#: comparisons, manage competitors and matches, and run the plugin's own
#: live price check. Price comparison routes are
#: deliberately gated by `alerts:read`, so omitting it makes the plugin's
#: engine-price refresh fail 403 after an otherwise successful pairing.
#:
#: `jobs:read` + `jobs:write` (review R03, 2026-09-09) are the live check.
#: The plugin's "refresh prices now" action posts
#: `POST /v1/variants/{id}/rescrape` (`jobs:write`) and then polls
#: `GET /v1/jobs/{job_id}` (`jobs:read`); with neither scope on the key,
#: a correctly paired plugin got 403 on a SHIPPED feature, every time.
#: `tests/integration/test_api_contract.py` recorded that gap deliberately
#: and asked for a decision rather than a scope-list edit, so here is the
#: decision and its reasoning: the risk that kept `jobs:write` off this
#: key is that it spends scrape budget from a credential living on a
#: merchant's own WordPress host — but both routes resolve every row
#: through `scoped_get(..., principal.workspace_id)`, so the spend is
#: bounded to the one workspace the key was minted for and a
#: cross-workspace id answers 404, not 403. See
#: `tests/unit/test_connector_key_live_checks.py`, which proves both
#: halves; the negative half is what makes the grant defensible.
#:
#: `jobs:cancel` stays OUT: it terminalizes rows nobody can get back and
#: no plugin feature needs it. So does everything else in
#: BOOTSTRAP_SCOPES — refresh_rules/webhooks/scrape_profiles/domain_rules
#: writes — which is SaaS-worker business and never belongs on a key that
#: lives in WordPress.
#:
#: Widening this list does NOT widen keys already minted: the scope set is
#: copied onto the `api_keys` row at mint time (see
#: `create_connector_key`), so an existing connector key keeps the set it
#: was issued with until the SaaS re-mints it. That is the safe direction,
#: and the operational cost of this change — every store paired before this
#: deploy keeps 403-ing on the live check until its key is REPLACED. There
#: is no in-place scope upgrade and there must not be one: mutating
#: `api_keys.scopes` would widen a credential whose holder never
#: re-consented and leave no audit row saying when. The deliberate
#: migration is mint-then-revoke, driven from the SaaS (the only component
#: that can persist the new plaintext) — see
#: `docs/ops/CONNECTOR_KEY_REISSUE.md` for the three paths (merchant
#: re-pair, single-store operator reissue, fleet sweep) and for how to tell
#: a stale key from a current one (`GET
#: /v1/admin/workspaces/{id}/api-keys` reports `scopes`).
CONNECTOR_SCOPES: list[str] = [
    "products:read",
    "variants:read",
    "competitors:read",
    "competitors:write",
    "matches:read",
    "matches:write",
    "alerts:read",
    "jobs:read",
    "jobs:write",
]


def get_admin_session() -> Iterator[Session]:
    """Session seam for the admin surface — BYPASSRLS, cross-workspace.

    A separate dependency (rather than calling `get_auth_session()`
    inline) so tests can override it the same way they override
    `get_current_principal` for tenant routers.
    """
    with get_auth_session() as session:
        yield session
        session.commit()


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _slugify(name: str) -> str:
    """A DISPLAY-ONLY slug derived from the name.

    Identity is `workspaces.external_ref` (UNIQUE), never the slug: the old
    `name-ref` slug let two different (name, ref) splits that join to the
    same string (`a-b`+`c` vs `a`+`b-c`) collide. Slug uniqueness is
    only a DB constraint; `_unique_slug` resolves collisions with `-2`, `-3`.
    """
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    base = "-".join(part for part in base.split("-") if part) or "workspace"
    return base[:190]


def _unique_slug(session: Session, name: str) -> str:
    base = _slugify(name)
    candidate, n = base, 1
    while session.execute(  # noqa: workspace-scope
        select(Workspace).where(Workspace.slug == candidate)
    ).scalars().first() is not None:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _workspace_by_ref(session: Session, external_ref: str) -> Workspace | None:
    return session.execute(  # noqa: workspace-scope
        select(Workspace).where(Workspace.external_ref == external_ref)
    ).scalars().first()


BOOTSTRAP_KEY_NAME_PREFIX = "saas-bootstrap:"


class _AmbiguousLegacyRef(Exception):
    pass


def _legacy_workspace_for_ref(session: Session, external_ref: str) -> Workspace | None:
    """A pre-E5 workspace (``external_ref IS NULL``) provisioned for this ref.

    Until the owner backfill of ``workspaces.external_ref`` runs, every
    workspace created before E5 has ``external_ref`` NULL, so a SaaS
    re-provision of an existing project would miss `_workspace_by_ref` and
    silently create a second workspace. The legacy identity is the bootstrap
    key the old code minted in the same transaction as the workspace, named
    ``saas-bootstrap:<raw external_ref>`` (exact, un-normalised, so
    ``Store_1``/``store.1`` and boundary splits stay distinct; API key rows
    are revoked, never deleted). The old ``name-ref`` slug is NOT used: it is
    the ambiguous identity E5 removes.

    Raises `_AmbiguousLegacyRef` when more than one legacy workspace carries
    that key (an old rename-and-retry); the caller refuses rather than guess.
    """
    workspace_ids = {
        key.workspace_id
        for key in session.execute(  # noqa: workspace-scope
            select(ApiKey).where(ApiKey.name == f"{BOOTSTRAP_KEY_NAME_PREFIX}{external_ref}")
        ).scalars().all()
    }
    if not workspace_ids:
        return None
    candidates = [
        ws
        for ws in session.execute(  # noqa: workspace-scope
            select(Workspace).where(Workspace.id.in_(workspace_ids))
        ).scalars().all()
        if ws.external_ref is None
    ]
    if len(candidates) > 1:
        raise _AmbiguousLegacyRef(external_ref)
    return candidates[0] if candidates else None


def _duplicate_ref(external_ref: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": {"code": "DUPLICATE_EXTERNAL_REF", "message": message}},
    )


@router.post("/workspaces", response_model=WorkspaceProvisionResponse, status_code=201)
def provision_workspace(
    payload: WorkspaceProvisionRequest,
    response: Response,
    session: Session = Depends(get_admin_session),
) -> WorkspaceProvisionResponse:
    """`POST /v1/admin/workspaces` — idempotent on a unique `external_ref`.

    The workspace is looked up by `external_ref` FIRST. If it exists the
    existing workspace is returned with 200 and `api_key: null`: no second
    bootstrap key is minted (the plaintext is unrecoverable by design, and
    a retry must never widen the set of live keys). Only when it is absent
    is a workspace created (201) with a bootstrap key whose plaintext is
    returned exactly once (only its prefix and sha256 hash are persisted).

    On a miss, a pre-E5 workspace (``external_ref IS NULL``) whose
    bootstrap key is named for this exact ref is adopted: its
    ``external_ref`` is stamped and it is returned (200, no new key), so a
    SaaS re-provision before the owner backfill never forks a second
    workspace. More than one such legacy workspace is a 409.

    The slug is display-only; collisions get `-2`, `-3`, ...
    """
    existing = _workspace_by_ref(session, payload.external_ref)
    if existing is None:
        try:
            legacy = _legacy_workspace_for_ref(session, payload.external_ref)
        except _AmbiguousLegacyRef as exc:
            raise _duplicate_ref(
                payload.external_ref,
                "More than one pre-existing workspace was provisioned for external_ref "
                f"{payload.external_ref!r}; an operator must backfill workspaces.external_ref.",
            ) from exc
        if legacy is not None:
            # Adopt the legacy workspace: stamp its ref so later calls hit the
            # unique lookup. A concurrent adopter/creator wins via the UNIQUE.
            legacy.external_ref = payload.external_ref
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                legacy = _workspace_by_ref(session, payload.external_ref)
                if legacy is None:
                    raise _duplicate_ref(
                        payload.external_ref,
                        "Workspace provisioning conflicted for external_ref "
                        f"{payload.external_ref!r}; retry.",
                    ) from None
            existing = legacy
    if existing is not None:
        response.status_code = 200
        return WorkspaceProvisionResponse(
            workspace_id=existing.id,
            api_key=None,
            external_ref=payload.external_ref,
        )

    workspace = Workspace(
        name=payload.name,
        slug=_unique_slug(session, payload.name),
        external_ref=payload.external_ref,
        status=WorkspaceStatus.ACTIVE,
    )
    session.add(workspace)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        # A concurrent provision of the same ref won the race: converge on it.
        winner = _workspace_by_ref(session, payload.external_ref)
        if winner is not None:
            response.status_code = 200
            return WorkspaceProvisionResponse(
                workspace_id=winner.id, api_key=None, external_ref=payload.external_ref
            )
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "DUPLICATE_EXTERNAL_REF",
                    "message": (
                        "Workspace provisioning conflicted for external_ref "
                        f"{payload.external_ref!r}; retry."
                    ),
                }
            },
        ) from exc

    full_secret, key_prefix, key_hash = generate_api_key()
    session.add(
        ApiKey(
            workspace_id=workspace.id,
            name=f"{BOOTSTRAP_KEY_NAME_PREFIX}{payload.external_ref}",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=BOOTSTRAP_SCOPES,
            status=ApiKeyStatus.ACTIVE,
        )
    )
    session.flush()

    return WorkspaceProvisionResponse(
        workspace_id=workspace.id,
        api_key=full_secret,
        external_ref=payload.external_ref,
    )


@router.post(
    "/workspaces/{workspace_id}/connector-keys",
    response_model=ConnectorKeyCreateResponse,
    status_code=201,
)
def create_connector_key(
    workspace_id: uuid.UUID,
    payload: ConnectorKeyCreateRequest,
    session: Session = Depends(get_admin_session),
) -> ConnectorKeyCreateResponse:
    """`POST /v1/admin/workspaces/{workspace_id}/connector-keys` -- mint a
    key scoped to `CONNECTOR_SCOPES` for a store connector that lives in
    WordPress (audit P0.3).

    Unlike `create_workspace_api_key` above, the scope set is NOT
    caller-suppliable here: a connector key is always exactly
    `CONNECTOR_SCOPES`, never wider, so WordPress can never end up
    holding `webhooks:write`/`scrape_profiles:write`/etc. by passing
    `scopes` in the request body -- there is no such field on
    `ConnectorKeyCreateRequest`. (`jobs:write` used to be named here as
    an example of something WordPress must never hold; review R03 granted
    it deliberately, bounded by `scoped_get(..., workspace_id)` on both
    live-check routes. See the `CONNECTOR_SCOPES` rationale above.)

    `scopes=list(CONNECTOR_SCOPES)` COPIES the list onto the row rather
    than referencing it, so a later widening of the constant never
    retroactively widens a key already living on a merchant's host --
    and reissuing those keys is therefore a deliberate operation, not a
    deploy side effect (`docs/ops/CONNECTOR_KEY_REISSUE.md`).

    404s on an unknown `workspace_id`, same rationale as
    `create_workspace_api_key`: the caller is the trusted,
    service-token-holding SaaS control plane.

    The plaintext key is returned exactly once and never persisted --
    same contract as `provision_workspace` above.
    """
    workspace = session.execute(
        select(Workspace).where(Workspace.id == workspace_id)  # noqa: workspace-scope
    ).scalar_one_or_none()
    if workspace is None:
        raise _not_found("Workspace not found.")

    full_secret, key_prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        workspace_id=workspace_id,
        name=f"connector:{payload.connector}:{workspace_id}",
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=list(CONNECTOR_SCOPES),
        status=ApiKeyStatus.ACTIVE,
    )
    session.add(api_key)
    session.flush()

    return ConnectorKeyCreateResponse(
        key_id=api_key.id,
        api_key=full_secret,  # returned exactly once; never persisted/re-shown
        key_prefix=key_prefix,
    )


@router.post(
    "/api-keys/{key_id}/revoke",
    response_model=ApiKeyRevokeResponse,
)
def revoke_api_key(
    key_id: uuid.UUID,
    session: Session = Depends(get_admin_session),
) -> ApiKeyRevokeResponse:
    """`POST /v1/admin/api-keys/{key_id}/revoke` -- revoke any
    admin-minted key by id (audit P0.3, primarily used to revoke a
    connector key on unpair).

    Not workspace-scoped in the path (unlike
    `revoke_workspace_api_key`'s `DELETE
    .../workspaces/{workspace_id}/api-keys/{api_key_id}`) -- the SaaS
    control plane tracks the connector key id directly and this is the
    trusted, service-token-gated admin surface, so a plain lookup by id
    is safe here. 404s on an unknown id via `_not_found`, mirroring
    `create_connector_key`/`create_workspace_api_key` above (contrast
    `revoke_workspace_api_key`'s deliberate 204-always idempotency,
    which exists to avoid leaking cross-workspace existence to a path
    with a workspace_id segment -- this route has none).

    Only sets `revoked_at` on the first revocation, mirroring
    `revoke_workspace_api_key`, so a redundant revoke is a no-op rather
    than clobbering the original revocation timestamp.
    """
    existing = session.execute(
        select(ApiKey).where(ApiKey.id == key_id)  # noqa: workspace-scope
    ).scalar_one_or_none()
    if existing is None:
        raise _not_found("API key not found.")

    if existing.status != ApiKeyStatus.REVOKED:
        existing.status = ApiKeyStatus.REVOKED
        existing.revoked_at = datetime.now(timezone.utc)
        session.flush()

    return ApiKeyRevokeResponse(key_id=existing.id, status=existing.status)


@router.post(
    "/workspaces/{workspace_id}/archive", response_model=WorkspaceArchiveResponse
)
def archive_workspace(
    workspace_id: uuid.UUID,
    session: Session = Depends(get_admin_session),
) -> WorkspaceArchiveResponse:
    """`POST /v1/admin/workspaces/{id}/archive` — pause + retention flag.

    Idempotent: archiving an already-archived (`SUSPENDED`) workspace is
    a 200, not an error.
    """
    workspace = session.execute(
        select(Workspace).where(Workspace.id == workspace_id)  # noqa: workspace-scope
    ).scalar_one_or_none()
    if workspace is None:
        raise _not_found("Workspace not found.")

    workspace.status = WorkspaceStatus.SUSPENDED
    session.flush()
    return WorkspaceArchiveResponse(
        workspace_id=workspace.id, status=str(workspace.status)
    )


@router.patch(
    "/workspaces/{workspace_id}/competitors/{competitor_id}/approval",
    response_model=CompetitorApprovalResponse,
)
def set_competitor_approval(
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    payload: CompetitorApprovalRequest,
    session: Session = Depends(get_admin_session),
) -> CompetitorApprovalResponse:
    """Operator-only: set `robots_policy` / `legal_status` (E7).

    Tenant routes reject `IGNORE_AFTER_APPROVAL` / `APPROVED` with 403.
    """
    competitor = session.execute(
        select(Competitor).where(  # noqa: workspace-scope
            Competitor.id == competitor_id, Competitor.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if competitor is None:
        raise _not_found("Competitor not found.")
    if payload.robots_policy is not None:
        competitor.robots_policy = payload.robots_policy
    if payload.legal_status is not None:
        competitor.legal_status = payload.legal_status
    session.flush()
    return CompetitorApprovalResponse(
        competitor_id=competitor.id,
        robots_policy=competitor.robots_policy,
        legal_status=competitor.legal_status,
    )


@router.post(
    "/workspaces/{workspace_id}/scrape-jobs/{scrape_job_id}/reconcile-false-failures",
    response_model=TargetReconciliationResponse,
)
def reconcile_false_failed_targets(
    workspace_id: uuid.UUID,
    scrape_job_id: uuid.UUID,
    payload: TargetReconciliationRequest,
    session: Session = Depends(get_admin_session),
) -> TargetReconciliationResponse:
    """Preview or apply the reviewed 2026-08-24 lifecycle reconciliation.

    Apply calls must repeat the exact candidate IDs returned by a dry run and
    name the requesting operator.  The service locks and rechecks that set, so
    this endpoint cannot turn a stale preview into a broader mutation.
    """
    if not payload.dry_run and not payload.requested_by:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "REQUESTED_BY_REQUIRED",
                    "message": "requested_by is required when applying reconciliation.",
                }
            },
        )
    try:
        report = reconcile_successful_failed_targets(
            session,
            workspace_id=workspace_id,
            scrape_job_id=scrape_job_id,
            dry_run=payload.dry_run,
            expected_match_ids=payload.expected_match_ids,
        )
    except LookupError as exc:
        raise _not_found(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "RECONCILIATION_PREVIEW_STALE", "message": str(exc)}},
        ) from exc

    response = TargetReconciliationResponse.model_validate(report.as_dict())
    logger.info(
        "admin_target_reconciliation requested_by=%s report=%s",
        payload.requested_by,
        response.model_dump(mode="json"),
    )
    return response


@router.post(
    "/workspaces/{workspace_id}/api-keys",
    response_model=AdminApiKeyCreateResponse,
    status_code=201,
)
def create_workspace_api_key(
    workspace_id: uuid.UUID,
    payload: AdminApiKeyCreateRequest,
    session: Session = Depends(get_admin_session),
) -> AdminApiKeyCreateResponse:
    """`POST /v1/admin/workspaces/{workspace_id}/api-keys` -- mint a named,
    workspace-scoped key on the SaaS's behalf (PLAN §7.4, phase4-connect
    Task 2).

    404s on an unknown `workspace_id` (mirrors `archive_workspace` just
    above) rather than letting a bad id fall through to the `api_keys`
    FK constraint as an opaque `IntegrityError` -> 500. This is safe to
    do here (unlike the DELETE below): the caller is the trusted,
    service-token-holding SaaS control plane, not an untrusted customer,
    so confirming "that workspace id doesn't exist" leaks nothing a
    customer could exploit and helps the SaaS catch a stale
    `cmWorkspaceId` immediately instead of via a confusing 500.

    The plaintext key is returned exactly once and never persisted --
    same contract as `provision_workspace` above and
    `POST /v1/api-keys` (api_keys.py).
    """
    workspace = session.execute(
        select(Workspace).where(Workspace.id == workspace_id)  # noqa: workspace-scope
    ).scalar_one_or_none()
    if workspace is None:
        raise _not_found("Workspace not found.")

    requested_scopes = (
        payload.scopes if payload.scopes is not None else list(BOOTSTRAP_SCOPES)
    )
    try:
        scopes = validate_scopes(requested_scopes)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_SCOPES", "message": str(exc)}},
        ) from exc

    full_secret, key_prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        workspace_id=workspace_id,
        name=payload.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=scopes,
        status=ApiKeyStatus.ACTIVE,
    )
    session.add(api_key)
    session.flush()

    return AdminApiKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        scopes=list(api_key.scopes),
        status=api_key.status,
        created_at=api_key.created_at,
        api_key=full_secret,  # returned exactly once; never persisted/re-shown
    )


@router.get(
    "/workspaces/{workspace_id}/api-keys",
    response_model=AdminApiKeyListResponse,
)
def list_workspace_api_keys(
    workspace_id: uuid.UUID,
    session: Session = Depends(get_admin_session),
) -> AdminApiKeyListResponse:
    """`GET /v1/admin/workspaces/{workspace_id}/api-keys` -- never
    `key_hash`, never plaintext. `scoped_select` (not a bare
    `select(ApiKey)`) so one workspace's keys are never visible through
    another workspace's path id."""
    stmt = scoped_select(ApiKey, workspace_id).order_by(ApiKey.created_at, ApiKey.id)
    rows = session.execute(stmt).scalars().all()
    items = [
        AdminApiKeyListItem(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            scopes=list(row.scopes),
            status=row.status,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return AdminApiKeyListResponse(items=items)


@router.delete("/workspaces/{workspace_id}/api-keys/{api_key_id}", status_code=204)
def revoke_workspace_api_key(
    workspace_id: uuid.UUID,
    api_key_id: uuid.UUID,
    session: Session = Depends(get_admin_session),
) -> None:
    """`DELETE /v1/admin/workspaces/{workspace_id}/api-keys/{api_key_id}` --
    IDEMPOTENT, always 204. A missing key, an already-revoked key, and a
    key belonging to another workspace all return 204 -- same contract
    and rationale as `DELETE /v1/api-keys/{id}` (api_keys.py:149-171):
    revoking is a "make sure this key can't authenticate" instruction,
    not a "does this exact row exist under this exact workspace"
    question, and answering the latter would leak cross-workspace
    existence information to whichever caller guesses an id. Do NOT
    turn this into a 404.

    `scoped_get` (not `session.get`) filters by BOTH id and
    `workspace_id` -- a workspace can never revoke another workspace's
    key by guessing its id (the cross-workspace case above resolves to
    "not found" -> 204 no-op, never touching the other workspace's row).

    Mutates the ORM object directly (`existing.status = ...;
    session.flush()`) rather than issuing a Core `update(...)` statement
    (contrast `api_keys.py`'s `revoke_api_key`) -- both produce the same
    UPDATE against a real `Session`, but only the ORM-attribute form is
    observable by this router's own `get_admin_session` test double
    (`FakeOrmSession`, which evaluates `select`/`WHERE` but not a bare
    `update()` statement), and it mirrors `archive_workspace`'s existing
    get-then-mutate style in this same file.

    Only sets `revoked_at` on the first revocation (guarded by the
    status check) so a redundant revoke of an already-revoked key is a
    true no-op rather than clobbering the original revocation
    timestamp.
    """
    existing = scoped_get(session, ApiKey, api_key_id, workspace_id)
    if existing is None:
        return None

    if existing.status != ApiKeyStatus.REVOKED:
        existing.status = ApiKeyStatus.REVOKED
        existing.revoked_at = datetime.now(timezone.utc)
        session.flush()
    return None


@router.get("/usage", response_model=UsageListResponse)
def export_usage(
    since: datetime,
    until: datetime,
    cursor: str | None = None,
    limit: int | None = None,
    session: Session = Depends(get_admin_session),
) -> UsageListResponse:
    """`GET /v1/admin/usage` — the SaaS metering feed (PLAN §7.2).

    Cursor-paginated, idempotent, window capped at 31 days. The response
    field names are a frozen contract — see `app.schemas.admin.UsageRow`.
    """
    # A naive since/until reaches Postgres as a bare `timestamp`,
    # interpreted in the session TimeZone -- normalize before validating
    # and before it drives the query (review finding I6c).
    since, until = normalize_window(since, until)
    try:
        validate_window(since, until)
    except InvalidUsageWindow as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_WINDOW", "message": str(exc)}},
        ) from exc
    except UsageWindowTooLarge as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "WINDOW_TOO_LARGE", "message": str(exc)}},
        ) from exc

    after: UsageCursor | None = None
    if cursor is not None:
        try:
            after = decode_usage_cursor(cursor)
        except InvalidUsageCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc

    page_limit = clamp_usage_limit(limit)
    rows = list(
        session.execute(
            build_usage_query(
                since=since, until=until, after=after, limit=page_limit
            )
        ).all()
    )

    has_more = len(rows) > page_limit
    page = rows[:page_limit]
    # Build the validated items first and derive the cursor from those,
    # not from the raw row: Pydantic has already coerced `cycle_ts` to a
    # real `datetime` (rows can carry it as a string in tests), and
    # `encode_usage_cursor` requires `.isoformat()` to exist.
    items = [UsageRow.model_validate(row, from_attributes=True) for row in page]
    next_cursor = (
        encode_usage_cursor(
            UsageCursor(
                cycle_ts=items[-1].cycle_ts,
                workspace_id=items[-1].workspace_id,
                product_id=items[-1].product_id,
            )
        )
        if has_more and items
        else None
    )

    return UsageListResponse(items=items, next_cursor=next_cursor)


@router.post(
    "/profiles/{scrape_profile_id}/regex-unquarantine",
    response_model=ProfileRegexUnquarantineResponse,
)
def unquarantine_profile_regex(
    scrape_profile_id: uuid.UUID,
    session: Session = Depends(get_admin_session),
) -> ProfileRegexUnquarantineResponse:
    """`POST /v1/admin/profiles/{id}/regex-unquarantine` — release a regex quarantine (A2/F02).

    Clears both `regex_quarantined_at` and `regex_timeout_count`, so the
    extraction chain resumes running this profile's
    `price_regex`/`old_price_regex`/`currency_regex`/`stock_regex` and the
    profile starts again from a clean count rather than one timeout away
    from re-quarantine.

    Cross-workspace on purpose (this router is service-token-only and runs
    on the BYPASSRLS seam): a **global** profile — `workspace_id IS NULL` —
    is never auto-quarantined by any single tenant's scrape, so an operator
    here is the only party that can release one.

    Idempotent: releasing a profile that was not quarantined is a 200 with
    `was_quarantined=false`. An unknown id is a 404 — silently reporting
    success for a profile that does not exist would let a typo read as a
    completed remediation.
    """
    profile = session.execute(
        select(ScrapeProfile).where(ScrapeProfile.id == scrape_profile_id)  # noqa: workspace-scope
    ).scalar_one_or_none()
    if profile is None:
        raise _not_found("Scrape profile not found.")

    was_quarantined = profile.regex_quarantined_at is not None
    quarantined_at = profile.regex_quarantined_at
    timeout_count = profile.regex_timeout_count

    clear_regex_quarantine(session, scrape_profile_id)
    session.flush()
    logger.info(
        "admin_regex_unquarantine scrape_profile_id=%s was_quarantined=%s timeout_count=%s",
        scrape_profile_id,
        was_quarantined,
        timeout_count,
    )
    return ProfileRegexUnquarantineResponse(
        scrape_profile_id=scrape_profile_id,
        was_quarantined=was_quarantined,
        quarantined_at=quarantined_at,
        regex_timeout_count=timeout_count,
    )
