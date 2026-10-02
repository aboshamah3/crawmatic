"""Bearer auth for the read-only catalog index routes.

`/v1/admin/index/lookup` and `/v1/admin/index/status` accept EITHER
`INDEX_SERVICE_TOKEN` (a lookup-only caller, e.g. the outreach service)
OR `SAAS_SERVICE_TOKEN` (the SaaS control plane). The workspace-addressed
index routes write matches and stay on `app.service_auth.
require_service_token` alone, so the index token can never write.

Same rules as `app.service_auth`: constant-time comparison, fail-closed
when no token is configured, and one uniform 401 for every failure.
"""

from __future__ import annotations

import hmac

from fastapi import Header

from app_shared.config import get_settings

from app.errors import auth_failed_exception

_BEARER = "Bearer "


def require_index_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: authorize a catalog index reader, or 401."""
    settings = get_settings()
    accepted = [t for t in (settings.INDEX_SERVICE_TOKEN, settings.SAAS_SERVICE_TOKEN) if t]
    if not accepted or not authorization or not authorization.startswith(_BEARER):
        raise auth_failed_exception()
    presented = authorization[len(_BEARER) :].strip().encode("utf-8", "surrogateescape")
    if not presented:
        raise auth_failed_exception()
    matched = False
    for token in accepted:
        # No early exit: every configured token is compared every time.
        matched |= hmac.compare_digest(presented, token.encode("utf-8", "surrogateescape"))
    if not matched:
        raise auth_failed_exception()
    return None
