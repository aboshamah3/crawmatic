"""Resolution-cache VALUE codec unit tests (SPEC-07 tasks.md T055).

`app_shared.profiles.resolution.encode_group_result`/`decode_group_result`
(+ `CACHE_NONE_MARKER`/`CACHE_FIELD_SEP`) is the single shared definition
`apps/api/app/services/profile_resolution.py` (the SPEC-06 orchestrator
that populates the Redis resolution cache) and
`apps/scrapers/price_monitor/spiders/generic_price_spider.py` (which reads
that same warm cache) both import -- previously each carried its own
byte-for-byte copy. Pure, no Redis.
"""

from __future__ import annotations

import uuid

from app_shared.profiles.resolution import (
    CACHE_FIELD_SEP,
    CACHE_NONE_MARKER,
    NONE_RESOLVED,
    ResolvedProfile,
    decode_group_result,
    encode_group_result,
)


def test_encode_decode_round_trips_a_resolved_profile() -> None:
    profile_id = uuid.uuid4()
    result = ResolvedProfile(profile_id=profile_id, level="competitor")

    encoded = encode_group_result(result)
    decoded = decode_group_result(encoded)

    assert decoded == result


def test_encode_decode_round_trips_none_resolved() -> None:
    encoded = encode_group_result(NONE_RESOLVED)
    decoded = decode_group_result(encoded)

    assert encoded == CACHE_NONE_MARKER
    assert decoded is NONE_RESOLVED


def test_wire_format_is_unchanged_from_the_pre_hoist_shape() -> None:
    """Byte-identical wire format (SPEC-07 tasks.md T055 "keep behavior
    byte-identical so existing cache entries still decode") -- pins the
    exact `f"{profile_id}{sep}{level}"` shape independently of the
    functions under test, so a future refactor that silently changes the
    encoding is caught here even if `encode`/`decode` still round-trip
    each other."""
    profile_id = uuid.uuid4()
    result = ResolvedProfile(profile_id=profile_id, level="workspace")

    encoded = encode_group_result(result)

    assert encoded == f"{profile_id}{CACHE_FIELD_SEP}workspace"
    assert CACHE_FIELD_SEP == "|"
    assert CACHE_NONE_MARKER == "none"


def test_decode_a_previously_written_cache_entry_still_works() -> None:
    """A value written under the pre-hoist copy (same literal wire format)
    must still decode correctly post-hoist -- existing Redis entries are
    not invalidated by this refactor."""
    profile_id = uuid.uuid4()
    legacy_encoded = f"{profile_id}|global"

    decoded = decode_group_result(legacy_encoded)

    assert decoded == ResolvedProfile(profile_id=profile_id, level="global")
