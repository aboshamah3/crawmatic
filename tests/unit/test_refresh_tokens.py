"""Unit tests for refresh-token primitives (SPEC-03 T022, FR-008/009/010/011).

`app_shared.security.tokens` — no DB/Redis required. The atomic rotation
SQL itself is exercised as pure string-shape assertions plus an
in-memory stand-in that mirrors its ``WHERE`` predicate (rotated/expired/
revoked -> rejected), since the real compare-and-swap semantics need a
live Postgres row lock (deferred to T026).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from app_shared.security.tokens import (
    ROTATE_REFRESH_TOKEN_SQL,
    REVOKE_REFRESH_TOKEN_SQL,
    generate_refresh_token,
    hash_token,
    verify_token_hash,
)


def test_generate_refresh_token_is_high_entropy_and_reasonably_long() -> None:
    raw, _ = generate_refresh_token()
    assert len(raw) >= 32
    # Two generations never collide in practice.
    raw2, _ = generate_refresh_token()
    assert raw != raw2


def test_generate_refresh_token_returns_matching_hash() -> None:
    raw, token_hash = generate_refresh_token()
    assert token_hash == hash_token(raw)


def test_hash_token_is_deterministic_sha256() -> None:
    raw = "fixed-value-for-hash-check"
    assert hash_token(raw) == hash_token(raw)
    assert len(hash_token(raw)) == 64  # sha256 hex digest length
    assert all(c in "0123456789abcdef" for c in hash_token(raw))


def test_verify_token_hash_true_for_match_false_for_mismatch() -> None:
    raw, token_hash = generate_refresh_token()
    assert verify_token_hash(raw, token_hash) is True
    assert verify_token_hash("some-other-raw-value", token_hash) is False


def test_rotation_sql_shape_matches_the_atomic_caller_contract() -> None:
    assert "UPDATE refresh_tokens" in ROTATE_REFRESH_TOKEN_SQL
    assert "SET revoked_at = now()" in ROTATE_REFRESH_TOKEN_SQL
    assert "token_hash = :token_hash" in ROTATE_REFRESH_TOKEN_SQL
    assert "revoked_at IS NULL" in ROTATE_REFRESH_TOKEN_SQL
    assert "expires_at > now()" in ROTATE_REFRESH_TOKEN_SQL
    assert "RETURNING id, user_id" in ROTATE_REFRESH_TOKEN_SQL


def test_revoke_sql_shape_is_idempotent_by_predicate() -> None:
    assert "UPDATE refresh_tokens" in REVOKE_REFRESH_TOKEN_SQL
    assert "SET revoked_at = now()" in REVOKE_REFRESH_TOKEN_SQL
    assert "token_hash = :token_hash" in REVOKE_REFRESH_TOKEN_SQL
    assert "revoked_at IS NULL" in REVOKE_REFRESH_TOKEN_SQL
    # No expiry check on the logout path -- an expired-but-unrevoked
    # token is still revoked so it can never later be replayed.
    assert "RETURNING" not in REVOKE_REFRESH_TOKEN_SQL


# --- In-memory stand-in exercising the rotation predicate as pure logic ---


@dataclass(frozen=True)
class _FakeRefreshRow:
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None


def _apply_rotation_predicate(row: _FakeRefreshRow | None, *, presented_hash: str, now: datetime):
    """Mirror ROTATE_REFRESH_TOKEN_SQL's WHERE clause against an in-memory row.

    Returns the "RETURNING" row on a match (and marks it revoked, like the
    real UPDATE would), or None on no match -- exactly the predicate the
    live SQL enforces atomically at the DB row-lock level.
    """
    if row is None or row.token_hash != presented_hash:
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at <= now:
        return None
    return replace(row, revoked_at=now)


def test_rotation_predicate_accepts_a_live_unrotated_token() -> None:
    now = datetime.now(timezone.utc)
    row = _FakeRefreshRow(token_hash="h", expires_at=now + timedelta(days=1), revoked_at=None)

    result = _apply_rotation_predicate(row, presented_hash="h", now=now)

    assert result is not None
    assert result.revoked_at == now


def test_rotation_predicate_rejects_already_rotated_token() -> None:
    now = datetime.now(timezone.utc)
    row = _FakeRefreshRow(
        token_hash="h", expires_at=now + timedelta(days=1), revoked_at=now - timedelta(seconds=1)
    )

    assert _apply_rotation_predicate(row, presented_hash="h", now=now) is None


def test_rotation_predicate_rejects_expired_token() -> None:
    now = datetime.now(timezone.utc)
    row = _FakeRefreshRow(
        token_hash="h", expires_at=now - timedelta(seconds=1), revoked_at=None
    )

    assert _apply_rotation_predicate(row, presented_hash="h", now=now) is None


def test_rotation_predicate_rejects_unknown_token() -> None:
    now = datetime.now(timezone.utc)

    assert _apply_rotation_predicate(None, presented_hash="does-not-exist", now=now) is None
