"""Single-workspace same-URL coalescing unit tests (EPA W4.3, report §8).

`app_shared.jobs.coalescing` — pure, no DB/Redis/network. Per the plan:
identical canonical URLs within one workspace cluster together (Step 1);
a different market/variant (a different `canonical_url_hash`) never
coalesces (Step 2); the cost split invariant this composes with
(`allocate_cost_largest_remainder`, C1) stays deterministic; the
freshness-window predicate reserved for a future cache-reuse pass is
correct at its boundaries.

`plan_batches` itself is untouched by W4.3 (see `coalescing.py`'s module
docstring) -- `tests/unit/test_jobs_batching.py` (pre-existing, not
modified here) is what pins "flag OFF = byte-identical planning": since
`cluster_for_coalescing` is a pure reorder that this suite never asks
`plan_batches` to apply implicitly, every one of those pre-existing
assertions continues to hold unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app_shared.enums import ScrapeProfileMode
from app_shared.jobs.batching import ResolvedTarget, plan_batches
from app_shared.jobs.coalescing import (
    cluster_for_coalescing,
    coalesced_groups,
    is_within_freshness_window,
    urls_by_match_id,
)
from app_shared.models.network_operations import (
    FRACTION_SCALE,
    allocate_cost_largest_remainder,
)
from app_shared.netledger.recorder import canonical_url_hash


def _target(
    *,
    domain: str = "shop.example.com",
    mode: ScrapeProfileMode = ScrapeProfileMode.HTTP,
    url: str | None = None,
    match_id: uuid.UUID | None = None,
) -> ResolvedTarget:
    return ResolvedTarget(
        match_id=match_id or uuid.uuid4(),
        competitor_domain=domain,
        mode=mode,
        canonical_url_hash=canonical_url_hash(url) if url is not None else None,
    )


# --- Step 1: identical canonical URLs within one workspace cluster ---------


def test_identical_canonical_url_targets_land_in_one_group() -> None:
    """Same competitor page, three separate matches -> one coalesced group."""
    shared_url = "https://shop.example.com/p/widget"
    targets = [_target(url=shared_url) for _ in range(3)]

    groups = coalesced_groups(targets)

    assert len(groups) == 1
    (match_ids,) = groups.values()
    assert set(match_ids) == {t.match_id for t in targets}


def test_cluster_for_coalescing_makes_same_url_targets_contiguous() -> None:
    """Interleaved input -> same-hash targets adjacent after clustering."""
    url_a = "https://shop.example.com/p/a"
    url_b = "https://shop.example.com/p/b"
    a1, b1, a2, b2, a3 = (
        _target(url=url_a),
        _target(url=url_b),
        _target(url=url_a),
        _target(url=url_b),
        _target(url=url_a),
    )
    interleaved = [a1, b1, a2, b2, a3]

    clustered = cluster_for_coalescing(interleaved)

    hashes = [t.canonical_url_hash for t in clustered]
    # Every run of consecutive equal hashes forms exactly one block --
    # i.e. no hash value reappears after a different hash has been seen.
    seen_and_closed: set[str] = set()
    previous = None
    for h in hashes:
        if h != previous and previous is not None:
            seen_and_closed.add(previous)
        assert h not in seen_and_closed, f"hash {h!r} split into two blocks: {hashes}"
        previous = h


def test_cluster_for_coalescing_keeps_full_match_id_set_and_no_duplicates() -> None:
    targets = [_target(url="https://shop.example.com/p/x") for _ in range(4)] + [
        _target(url="https://shop.example.com/p/y") for _ in range(2)
    ]

    clustered = cluster_for_coalescing(targets)

    assert len(clustered) == len(targets)
    assert {t.match_id for t in clustered} == {t.match_id for t in targets}


def test_coalescing_feeds_plan_batches_without_changing_match_id_cardinality() -> None:
    """Clustering only reorders `plan_batches`'s input -- `batch.match_ids`
    still names every match; only the downstream fetch count can drop
    once the spider-level `SCRAPE_URL_DEDUP` mechanism also runs.
    """
    shared_url = "https://shop.example.com/p/shared"
    coalescible = [_target(url=shared_url) for _ in range(5)]
    distinct = [_target(url=f"https://shop.example.com/p/{i}") for i in range(3)]
    targets = coalescible + distinct

    clustered = cluster_for_coalescing(targets)
    batches = plan_batches(clustered, http_min=1, http_max=200)

    assert len(batches) == 1
    assert set(batches[0].match_ids) == {t.match_id for t in targets}
    assert len(batches[0].match_ids) == 8


def test_coalescing_never_splits_cluster_across_a_chunk_boundary_when_it_fits() -> None:
    """The concrete bug this closes: without clustering, chunking at
    `http_max` could split a same-URL cluster across two chunks purely by
    accident of input order; after clustering it cannot, as long as the
    cluster itself is not larger than the chunk ceiling.
    """
    shared_url = "https://shop.example.com/p/shared"
    # Position the 4-member cluster straddling where a naive http_max=5
    # chunk boundary would fall (indices 3-6 in unclustered order).
    singles = [_target(url=f"https://shop.example.com/p/s{i}") for i in range(3)]
    cluster = [_target(url=shared_url) for _ in range(4)]
    more_singles = [_target(url=f"https://shop.example.com/p/t{i}") for i in range(3)]
    unclustered_order = singles + cluster + more_singles

    # Unclustered: naive slicing at http_max=5 DOES split the cluster.
    naive_batches = plan_batches(unclustered_order, http_min=1, http_max=5)
    naive_cluster_batch_indices = {
        b.batch_index
        for b in naive_batches
        for mid in b.match_ids
        if mid in {t.match_id for t in cluster}
    }
    assert len(naive_cluster_batch_indices) > 1, (
        "test setup assumption failed: the naive order was expected to split "
        "the cluster across a chunk boundary"
    )

    # Clustered: the same targets, reordered, never split the cluster.
    clustered = cluster_for_coalescing(unclustered_order)
    clustered_batches = plan_batches(clustered, http_min=1, http_max=5)
    clustered_cluster_batch_indices = {
        b.batch_index
        for b in clustered_batches
        for mid in b.match_ids
        if mid in {t.match_id for t in cluster}
    }
    assert len(clustered_cluster_batch_indices) == 1


# --- Step 2: variant/region separation --------------------------------------


def test_different_variant_url_never_coalesces_with_the_base_url() -> None:
    """A region/variant query param changes the URL -> different canonical
    identity -> no coalesce, even for the same competitor+path."""
    base = _target(url="https://shop.example.com/p/widget")
    us_variant = _target(url="https://shop.example.com/p/widget?region=us")
    uk_variant = _target(url="https://shop.example.com/p/widget?region=uk")

    groups = coalesced_groups([base, us_variant, uk_variant])

    assert len(groups) == 3, "each distinct URL must form its own group"


def test_different_market_subdomain_never_coalesces() -> None:
    amazon_com = _target(url="https://shop.amazon.com/p/widget")
    amazon_co_uk = _target(url="https://shop.amazon.co.uk/p/widget")

    groups = coalesced_groups([amazon_com, amazon_co_uk])

    assert len(groups) == 2


def test_targets_with_no_canonical_identity_never_coalesce_with_each_other() -> None:
    """`None` must never be treated as a shared identity -- two targets
    that both lack a canonical hash are two singleton groups, not one."""
    a, b = _target(url=None), _target(url=None)

    groups = coalesced_groups([a, b])

    assert len(groups) == 2
    assert all(len(ids) == 1 for ids in groups.values())


def test_urls_by_match_id_reflects_attached_identity() -> None:
    url = "https://shop.example.com/p/widget"
    target = _target(url=url)

    view = urls_by_match_id([target])

    assert view[target.match_id] == canonical_url_hash(url)


# --- deterministic cost split (largest-remainder, C1) -----------------------


def test_single_workspace_coalesced_op_gets_exactly_one_full_allocation() -> None:
    """C1 uniformity rule: a single-workspace operation -- coalesced or
    not, one match or five -- gets exactly ONE allocation row at fraction
    1.0. The match count behind the fetch never fragments the workspace's
    own share; `network_operation_allocations` is keyed on (operation,
    workspace), never (operation, match)."""
    weights = [1]  # one workspace, however many matches rode the fetch

    fractions = allocate_cost_largest_remainder(FRACTION_SCALE, weights)
    amounts = allocate_cost_largest_remainder(9973, weights)

    assert fractions == [FRACTION_SCALE]
    assert amounts == [9973]


def test_largest_remainder_split_is_exact_and_deterministic() -> None:
    """The rounding rule the deferred allocation-total trigger assumes --
    pinned here because coalescing composes with it directly. Naive
    per-share rounding loses a unit on 100/3; largest-remainder must not.
    """
    weights = [1, 1, 1]

    first = allocate_cost_largest_remainder(100, weights)
    second = allocate_cost_largest_remainder(100, weights)

    assert sum(first) == 100
    assert sorted(first) == [33, 33, 34]
    assert first == second, "must be deterministic for a fixed input order"


# --- freshness-window predicate (future cache-reuse; canary-exercised) -----


def test_freshness_window_accepts_recent_operation() -> None:
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    reference = now - timedelta(seconds=60)

    assert is_within_freshness_window(reference, now, freshness_seconds=300) is True


def test_freshness_window_rejects_stale_operation() -> None:
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    reference = now - timedelta(seconds=301)

    assert is_within_freshness_window(reference, now, freshness_seconds=300) is False


def test_freshness_window_boundary_is_inclusive() -> None:
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    reference = now - timedelta(seconds=300)

    assert is_within_freshness_window(reference, now, freshness_seconds=300) is True


def test_freshness_window_zero_or_negative_is_always_stale() -> None:
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    reference = now  # would be "fresh" under any positive window

    assert is_within_freshness_window(reference, now, freshness_seconds=0) is False
    assert is_within_freshness_window(reference, now, freshness_seconds=-1) is False


def test_freshness_window_rejects_a_reference_in_the_future() -> None:
    now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    reference = now + timedelta(seconds=5)  # clock skew / bad caller

    assert is_within_freshness_window(reference, now, freshness_seconds=300) is False
