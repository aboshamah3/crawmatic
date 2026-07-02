"""Unit tests for the uniform auth-failure error builder (SPEC-03 T024, FR-006/SC-001).

`apps/api/app/errors.py`. The login handler calls this builder on every
failure path (unknown email, wrong password, suspended, ...) — asserting
the builder itself is byte-identical across calls is what guarantees no
route can accidentally leak which factor failed, since there is only one
code path that can produce the body.
"""

from __future__ import annotations

import json

from app.errors import (
    AUTH_FAILED_CODE,
    AUTH_FAILED_MESSAGE,
    auth_failed_body,
    auth_failed_exception,
    rate_limited_body,
)


def test_unknown_email_and_wrong_password_bodies_are_byte_identical() -> None:
    # Both "factors" call the exact same builder with no arguments -- there
    # is no parameter through which a caller could differentiate them.
    unknown_email_body = auth_failed_body()
    wrong_password_body = auth_failed_body()

    assert json.dumps(unknown_email_body, sort_keys=True) == json.dumps(
        wrong_password_body, sort_keys=True
    )


def test_auth_failed_body_shape() -> None:
    body = auth_failed_body()
    assert body == {"error": {"code": AUTH_FAILED_CODE, "message": AUTH_FAILED_MESSAGE}}


def test_auth_failed_exception_is_401_with_uniform_detail() -> None:
    exc = auth_failed_exception()
    assert exc.status_code == 401
    assert exc.detail == {"code": AUTH_FAILED_CODE, "message": AUTH_FAILED_MESSAGE}


def test_two_auth_failed_exceptions_carry_identical_status_and_detail() -> None:
    # Simulates two different failure call sites (e.g. one for unknown
    # email, one for wrong password) -- both must be indistinguishable.
    exc_a = auth_failed_exception()
    exc_b = auth_failed_exception()

    assert exc_a.status_code == exc_b.status_code
    assert exc_a.detail == exc_b.detail


def test_rate_limited_body_carries_no_factor_disclosure() -> None:
    body = rate_limited_body()
    assert body["error"]["code"] == "RATE_LIMITED"
    # No mention of which counter (account vs source) tripped.
    assert "account" not in json.dumps(body).lower()
    assert "source" not in json.dumps(body).lower()
    assert "ip" not in json.dumps(body).lower()
