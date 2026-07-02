"""Unit tests for argon2id password primitives (SPEC-03 T021, FR-005).

`app_shared.security.passwords` — no DB/Redis required. ``hash_password``
reads ``Settings.ARGON2_*`` via ``get_settings()`` (same lazy-singleton
config surface as the rest of ``app_shared``), so — matching the pattern
in ``tests/unit/test_auth_session.py`` — every test here monkeypatches
``get_settings`` with a minimal fake exposing only the ``ARGON2_*``
fields, rather than requiring the full required-env ``Settings`` (whose
unrelated fields like ``DATABASE_URL``/``SCRAPYD_*`` are irrelevant to
password hashing and not set in this environment).
"""

from __future__ import annotations

import pytest

import app_shared.security.passwords as passwords_module
from app_shared.security.passwords import (
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)


class _FakeSettings:
    ARGON2_TIME_COST: int | None = None
    ARGON2_MEMORY_COST: int | None = None
    ARGON2_PARALLELISM: int | None = None


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(passwords_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(passwords_module, "_dummy_hash", None, raising=False)


def test_hash_password_is_not_the_plaintext() -> None:
    plaintext = "correct horse battery staple"
    assert hash_password(plaintext) != plaintext


def test_hash_password_uses_a_random_salt() -> None:
    plaintext = "correct horse battery staple"
    assert hash_password(plaintext) != hash_password(plaintext)


def test_verify_password_round_trips() -> None:
    plaintext = "correct horse battery staple"
    stored = hash_password(plaintext)
    assert verify_password(stored, plaintext) is True


def test_verify_password_rejects_wrong_password() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password(stored, "wrong password") is False


def test_verify_password_never_raises_on_malformed_hash() -> None:
    assert verify_password("not-a-real-argon2-hash", "anything") is False


def test_needs_rehash_false_on_fresh_hash() -> None:
    stored = hash_password("correct horse battery staple")
    assert needs_rehash(stored) is False


def test_dummy_verify_does_not_raise_and_returns_none() -> None:
    assert dummy_verify("any-password-at-all") is None
    # Repeated calls are stable (reuses the cached dummy hash).
    assert dummy_verify("another-password") is None
