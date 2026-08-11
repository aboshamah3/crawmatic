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
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.database import get_auth_session
from app_shared.enums import ApiKeyStatus, WorkspaceStatus
from app_shared.models.identity import ApiKey, Workspace
from app_shared.repository import scoped_get, scoped_select
from app_shared.security.api_keys import generate_api_key
from app_shared.security.scopes import validate_scopes

from app.schemas.admin import (
    AdminApiKeyCreateRequest,
    AdminApiKeyCreateResponse,
    AdminApiKeyListItem,
    AdminApiKeyListResponse,
    UsageListResponse,
    UsageRow,
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


def _slugify(name: str, external_ref: str) -> str:
    """A unique, stable slug: the readable name plus the SaaS ref.

    `workspaces.slug` is UNIQUE; `external_ref` is unique per SaaS
    project, so appending it makes collisions between two customers
    named "Acme Store" impossible without a retry loop.

    Truncate `base`, never `ref`: `f"{base}-{ref}"[:200]` used to
    truncate from the END, which is where `external_ref` lives. Since
    `name` (and therefore `base`) can itself be up to 200 chars, two
    different refs on the same/similar long name collided into the
    IDENTICAL slug -- `_slugify("A"*200, "ref-alpha") ==
    _slugify("A"*200, "ref-beta")` -- so customer B's provisioning call
    would 409 `DUPLICATE_EXTERNAL_REF` naming their own ref against a
    workspace that actually belongs to customer A.
    """
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    base = "-".join(part for part in base.split("-") if part) or "workspace"
    ref = "".join(ch.lower() if ch.isalnum() else "-" for ch in external_ref).strip("-")
    base = base[: max(1, 200 - len(ref) - 1)]
    return f"{base}-{ref}"


@router.post("/workspaces", response_model=WorkspaceProvisionResponse, status_code=201)
def provision_workspace(
    payload: WorkspaceProvisionRequest,
    session: Session = Depends(get_admin_session),
) -> WorkspaceProvisionResponse:
    """`POST /v1/admin/workspaces` — create a workspace + bootstrap key.

    The plaintext key is returned exactly once and never stored; only
    its prefix and sha256 hash are persisted (same contract as
    `POST /v1/api-keys`).

    Re-provisioning an `external_ref` that already has a workspace is a
    `409 DUPLICATE_EXTERNAL_REF`, not a 500. The SaaS retries this call
    (network blip, job redelivery), and a retry must get a clear,
    actionable answer instead of an opaque server error. It is
    deliberately NOT idempotent-success: silently returning the existing
    workspace would have to either mint a second bootstrap key or return
    none, and both are worse than making the caller decide.
    """
    workspace = Workspace(
        name=payload.name,
        slug=_slugify(payload.name, payload.external_ref),
        status=WorkspaceStatus.ACTIVE,
    )
    session.add(workspace)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "DUPLICATE_EXTERNAL_REF",
                    "message": (
                        "A workspace already exists for external_ref "
                        f"{payload.external_ref!r}."
                    ),
                }
            },
        ) from exc

    full_secret, key_prefix, key_hash = generate_api_key()
    session.add(
        ApiKey(
            workspace_id=workspace.id,
            name=f"saas-bootstrap:{payload.external_ref}",
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
