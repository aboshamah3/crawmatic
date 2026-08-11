"""Static service-token auth for `/v1/admin/*` (PLAN §7.1).

Deliberately **not** part of `app.deps`: every `/v1` tenant endpoint
resolves exactly one authorized workspace context via
``get_current_principal``, whereas the admin surface is cross-workspace
by definition (it provisions new workspaces and aggregates usage over
all of them). Mixing the two seams would mean teaching the workspace
resolver about a principal with no workspace, which is precisely the
hole `deps._resolve_workspace` exists to close.

The token is compared with ``hmac.compare_digest`` so a wrong guess
costs the same time as a right one. When ``SAAS_SERVICE_TOKEN`` is unset
or empty the dependency refuses every request (fail-closed) — an engine
with no SaaS control plane exposes no admin surface.

Failures always raise the uniform ``auth_failed_exception`` (401,
``AUTH_FAILED``) — never a message distinguishing "no token configured"
from "wrong token", which would leak deployment state to an attacker.
"""

from __future__ import annotations

import hmac

from fastapi import Header

from app_shared.config import get_settings

from app.errors import auth_failed_exception

_BEARER = "Bearer "


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: authorize the SaaS control plane, or 401.

    Returns ``None`` on success — the admin routes need no principal,
    only the assurance that the caller holds the shared secret.
    """
    settings = get_settings()
    configured = settings.SAAS_SERVICE_TOKEN
    if not configured:
        raise auth_failed_exception()

    if not authorization or not authorization.startswith(_BEARER):
        raise auth_failed_exception()

    presented = authorization[len(_BEARER) :].strip()
    if not presented:
        raise auth_failed_exception()

    if not hmac.compare_digest(
        presented.encode("utf-8", "surrogateescape"),
        configured.encode("utf-8", "surrogateescape"),
    ):
        raise auth_failed_exception()

    return None
