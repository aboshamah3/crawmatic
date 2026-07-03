"""`app_shared/profiles/resolution.py::resolution_cache_key` unit tests
(SPEC-06 US3 T038, FR-019, SC-005).

Determinism + collision-freedom over `(workspace_id, competitor_id,
url_pattern)` tuples -- pure, no Redis.
"""

from __future__ import annotations

import uuid

from app_shared.profiles.resolution import resolution_cache_key


def test_resolution_cache_key_is_deterministic() -> None:
    ws = uuid.uuid4()
    competitor = uuid.uuid4()

    key_1 = resolution_cache_key(ws, competitor, "/p/{sku}")
    key_2 = resolution_cache_key(ws, competitor, "/p/{sku}")

    assert key_1 == key_2


def test_resolution_cache_key_deterministic_across_many_calls() -> None:
    ws = uuid.uuid4()
    competitor = uuid.uuid4()

    keys = {resolution_cache_key(ws, competitor, "/p/{sku}") for _ in range(50)}

    assert len(keys) == 1


def test_resolution_cache_key_differs_by_workspace() -> None:
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    competitor = uuid.uuid4()

    key_a = resolution_cache_key(ws_a, competitor, "/p/{sku}")
    key_b = resolution_cache_key(ws_b, competitor, "/p/{sku}")

    assert key_a != key_b


def test_resolution_cache_key_differs_by_competitor() -> None:
    ws = uuid.uuid4()
    competitor_a, competitor_b = uuid.uuid4(), uuid.uuid4()

    key_a = resolution_cache_key(ws, competitor_a, "/p/{sku}")
    key_b = resolution_cache_key(ws, competitor_b, "/p/{sku}")

    assert key_a != key_b


def test_resolution_cache_key_differs_by_url_pattern() -> None:
    ws = uuid.uuid4()
    competitor = uuid.uuid4()

    key_a = resolution_cache_key(ws, competitor, "/p/{sku}")
    key_b = resolution_cache_key(ws, competitor, "/other")

    assert key_a != key_b


def test_resolution_cache_key_bounded_length_for_long_url_pattern() -> None:
    ws = uuid.uuid4()
    competitor = uuid.uuid4()
    long_pattern = "/p/" + ("x" * 10_000)

    key = resolution_cache_key(ws, competitor, long_pattern)

    # url_pattern is hashed (sha1 hex = 40 chars), never embedded raw, so
    # the key length is bounded regardless of pattern size.
    assert len(key) < 200


def test_resolution_cache_key_collision_free_across_near_collision_tuples() -> None:
    """Guards against a naive concatenation bug where distinct tuples could
    collide by shifting a boundary between fields (e.g. `(ws="ab", comp="c")`
    vs `(ws="a", comp="bc")` colliding under plain string concatenation)."""
    ws_1, competitor_1 = uuid.uuid4(), uuid.uuid4()
    ws_2, competitor_2 = uuid.uuid4(), uuid.uuid4()

    tuples = [
        (ws_1, competitor_1, "/p/1"),
        (ws_1, competitor_1, "/p/11"),
        (ws_1, competitor_1, "1/p/1"),
        (ws_2, competitor_1, "/p/1"),
        (ws_1, competitor_2, "/p/1"),
        (ws_1, competitor_1, ""),
        (ws_1, competitor_1, "//p/1"),
    ]

    keys = [resolution_cache_key(*t) for t in tuples]

    assert len(keys) == len(set(keys))


def test_resolution_cache_key_accepts_string_ids_too() -> None:
    """`workspace_id`/`competitor_id` may be passed as `uuid.UUID` or `str`
    (callers may already hold string ids); the key format is stable either
    way for a given underlying id."""
    ws = uuid.uuid4()
    competitor = uuid.uuid4()

    key_uuid = resolution_cache_key(ws, competitor, "/p/{sku}")
    key_str = resolution_cache_key(str(ws), str(competitor), "/p/{sku}")

    assert key_uuid == key_str


def test_resolution_cache_key_has_expected_prefix() -> None:
    ws = uuid.uuid4()
    competitor = uuid.uuid4()

    key = resolution_cache_key(ws, competitor, "/p/{sku}")

    assert key.startswith("profres:")
