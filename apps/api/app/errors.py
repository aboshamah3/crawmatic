"""Uniform authentication-failure error builder (`contracts/api-auth.md`).

Every authentication failure path (unknown email, wrong password,
suspended user/workspace, expired/rotated/revoked refresh token, expired
or tampered access token) must emit the **identical** structured body and
status so no response leaks which factor was wrong (FR-006/SC-001). This
module is the single place that shape is defined, so every router calls
through it rather than constructing its own error body.
"""

from __future__ import annotations

from fastapi import HTTPException

AUTH_FAILED_CODE = "AUTH_FAILED"
AUTH_FAILED_MESSAGE = "Authentication failed."

RATE_LIMITED_CODE = "RATE_LIMITED"
RATE_LIMITED_MESSAGE = "Too many attempts. Please try again later."


def auth_failed_body() -> dict:
    """The uniform auth-failure JSON body — identical for every failure factor."""
    return {"error": {"code": AUTH_FAILED_CODE, "message": AUTH_FAILED_MESSAGE}}


def rate_limited_body() -> dict:
    """The rate-limited JSON body — also carries no factor disclosure."""
    return {"error": {"code": RATE_LIMITED_CODE, "message": RATE_LIMITED_MESSAGE}}


def auth_failed_exception() -> HTTPException:
    """A ``401`` carrying the uniform auth-failure body.

    Use for every login/refresh/access-token failure regardless of which
    factor (unknown email, wrong password, suspended, expired, rotated,
    revoked, tampered) actually caused it.
    """
    return HTTPException(status_code=401, detail=auth_failed_body()["error"])


def rate_limited_exception(retry_after_seconds: int | None = None) -> HTTPException:
    """A ``429`` for a throttled login attempt (no factor disclosure)."""
    headers = None
    if retry_after_seconds is not None:
        headers = {"Retry-After": str(retry_after_seconds)}
    return HTTPException(
        status_code=429, detail=rate_limited_body()["error"], headers=headers
    )
