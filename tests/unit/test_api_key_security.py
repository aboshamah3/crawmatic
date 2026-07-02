"""Unit tests for API-key crypto primitives (SPEC-03 T033, FR-012/FR-016).

`app_shared.security.api_keys` — stdlib-only, no DB/Redis required.
"""

from __future__ import annotations

from app_shared.security.api_keys import (
    API_KEY_PREFIX,
    generate_api_key,
    hash_api_key,
    parse_prefix,
    verify_api_key,
)


def test_generated_key_has_ck_prefix_and_is_high_entropy() -> None:
    full_secret, key_prefix, key_hash = generate_api_key()
    assert full_secret.startswith(API_KEY_PREFIX)
    # secrets.token_urlsafe(32) -> a long base64url string; the total
    # secret is comfortably longer than the prefix alone.
    assert len(full_secret) > len(API_KEY_PREFIX) + 32
    assert key_prefix.startswith(API_KEY_PREFIX)
    assert key_hash != full_secret


def test_two_generated_keys_are_distinct() -> None:
    secret_a, _, _ = generate_api_key()
    secret_b, _, _ = generate_api_key()
    assert secret_a != secret_b


def test_parse_prefix_round_trips_against_generated_prefix() -> None:
    full_secret, key_prefix, _ = generate_api_key()
    assert parse_prefix(full_secret) == key_prefix


def test_hash_api_key_is_deterministic_sha256() -> None:
    secret = "ck_abcdef1234567890"
    assert hash_api_key(secret) == hash_api_key(secret)

    import hashlib

    assert hash_api_key(secret) == hashlib.sha256(secret.encode("utf-8")).hexdigest()


def test_verify_api_key_true_for_matching_secret() -> None:
    full_secret, _, key_hash = generate_api_key()
    assert verify_api_key(full_secret, key_hash) is True


def test_verify_api_key_false_for_mismatched_secret() -> None:
    full_secret, _, key_hash = generate_api_key()
    other_secret, _, _ = generate_api_key()
    assert verify_api_key(other_secret, key_hash) is False


def test_two_keys_sharing_a_forced_prefix_verify_only_against_their_own_hash() -> None:
    # Force a shared key_prefix by construction (the natural collision
    # case FR-016 must survive): two distinct full secrets that happen to
    # produce the same short lookup prefix.
    shared_tail = "AAAAAA"
    secret_a = f"{API_KEY_PREFIX}{shared_tail}-key-one-suffix"
    secret_b = f"{API_KEY_PREFIX}{shared_tail}-key-two-suffix"
    assert parse_prefix(secret_a) == parse_prefix(secret_b)

    hash_a = hash_api_key(secret_a)
    hash_b = hash_api_key(secret_b)
    assert hash_a != hash_b

    # Each secret verifies only against its own hash, never the other's,
    # despite sharing a key_prefix.
    assert verify_api_key(secret_a, hash_a) is True
    assert verify_api_key(secret_a, hash_b) is False
    assert verify_api_key(secret_b, hash_b) is True
    assert verify_api_key(secret_b, hash_a) is False
