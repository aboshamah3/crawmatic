"""The coalescing equivalence key (EPA C4, F08 / plan §11 item 5).

W4.3 folded two matches onto one physical fetch when they shared a
``canonical_url_hash``. That is only half an identity, and the missing
half is the dangerous half: two matches can share a canonical URL and
still be *different fetches* — a different proxy exit country gets a
different storefront's prices, a different transport gets a different
response body, a different variant selector reads a different offer off
the same page. Coalescing those would not save a fetch, it would fan a
**wrong** price out to the siblings, which is strictly worse than paying
twice.

C4 widens the key to ``(workspace_id, canonical_url_hash, region,
currency, proxy_country, transport, variant_selector_hash)``. These tests
pin the four properties that makes true:

1. every added component SPLITS a cluster (the plan's own worked example:
   same URL, different ``proxy_country`` -> not coalesced);
2. an identical key still yields ONE physical fetch and every one of its
   logical results;
3. a caller that attaches none of the new components gets exactly the
   pre-C4 grouping, so this widening cannot regress an unadopted caller;
4. cross-workspace sharing stays OFF —
   ``CrossWorkspaceCoalescingUnsupported`` remains the contract, not a
   silent merge.
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.enums import ScrapeProfileMode
from app_shared.jobs.batching import ResolvedTarget, plan_batches
from app_shared.jobs.coalescing import (
    CoalescingKey,
    CrossWorkspaceCoalescingUnsupported,
    cluster_for_coalescing,
    coalesced_groups,
    coalescing_key,
)

WORKSPACE = uuid.uuid4()
URL_HASH = "sha256:the-same-canonical-url"


def _target(**overrides) -> ResolvedTarget:
    fields = {
        "match_id": uuid.uuid4(),
        "competitor_domain": "noon.com",
        "mode": ScrapeProfileMode.HTTP,
        "canonical_url_hash": URL_HASH,
        "workspace_id": WORKSPACE,
    }
    fields.update(overrides)
    return ResolvedTarget(**fields)


# --- 1. every component splits ----------------------------------------------


def test_same_url_different_proxy_country_is_not_coalesced() -> None:
    """The plan's worked example, and the most consequential splitter.

    An `SA` exit and an `AE` exit of the same noon.com URL routinely
    return different prices and availability. Folding them would publish
    one country's price under the other's match.
    """
    saudi = _target(proxy_country="SA")
    emirati = _target(proxy_country="AE")

    groups = coalesced_groups([saudi, emirati])

    assert len(groups) == 2, "a different proxy exit is a different fetch"
    assert coalescing_key(saudi) != coalescing_key(emirati)


@pytest.mark.parametrize(
    "field_name, left, right",
    [
        ("region", "sa", "ae"),
        ("currency", "SAR", "AED"),
        ("proxy_country", "SA", "AE"),
        ("transport", "PROXY_HTTP", "PLAYWRIGHT_PROXY"),
        ("variant_selector_hash", "variant-a", "variant-b"),
        ("canonical_url_hash", "hash-a", "hash-b"),
    ],
)
def test_each_component_splits_a_cluster(field_name, left, right) -> None:
    """No component of the key is decorative — each one alone splits."""
    groups = coalesced_groups(
        [_target(**{field_name: left}), _target(**{field_name: right})]
    )
    assert len(groups) == 2, f"{field_name} must be part of the equivalence key"


def test_a_component_present_on_one_side_only_still_splits() -> None:
    """`None` is not a wildcard.

    "We don't know this target's proxy country" must never be treated as
    "it matches whatever the other one has" — that is precisely how an
    unknown quietly becomes a wrong shared fetch.
    """
    known = _target(proxy_country="SA")
    unknown = _target(proxy_country=None)
    assert coalescing_key(known) != coalescing_key(unknown)
    assert len(coalesced_groups([known, unknown])) == 2


# --- 2. an identical key is one fetch, many logical results ------------------


def test_identical_key_yields_one_physical_fetch_and_every_logical_result() -> None:
    """Two matches, one fetch identity, both match_ids preserved.

    The saving is at the fetch layer; the accounting is not. A coalesced
    cluster still carries every one of its match_ids into the batch, so
    per-match observation/attempt/target rows stay 1:1 — only the
    downstream physical fetch count drops.
    """
    first = _target(proxy_country="SA", currency="SAR", transport="PROXY_HTTP")
    second = _target(proxy_country="SA", currency="SAR", transport="PROXY_HTTP")

    groups = coalesced_groups([first, second])

    assert len(groups) == 1, "one physical fetch"
    (match_ids,) = groups.values()
    assert sorted(match_ids) == sorted(
        [first.match_id, second.match_id]
    ), "two logical results"

    # ...and both survive planning, in the same batch.
    batches = plan_batches(cluster_for_coalescing([first, second]))
    assert len(batches) == 1
    assert sorted(batches[0].match_ids) == sorted([first.match_id, second.match_id])


def test_clustering_makes_same_key_targets_contiguous() -> None:
    """The whole point of the reorder: a cluster must not straddle a chunk."""
    a1 = _target(canonical_url_hash="hash-a")
    b1 = _target(canonical_url_hash="hash-b")
    a2 = _target(canonical_url_hash="hash-a")
    b2 = _target(canonical_url_hash="hash-b")

    ordered = cluster_for_coalescing([a1, b1, a2, b2])
    hashes = [target.canonical_url_hash for target in ordered]

    assert hashes == ["hash-a", "hash-a", "hash-b", "hash-b"]


# --- 3. an unadopted caller is unchanged ------------------------------------


def test_targets_with_no_new_components_group_exactly_as_before() -> None:
    """Pre-C4 callers attach only the URL hash — grouping must not move."""
    shared_hash = "hash-shared"
    first = ResolvedTarget(
        match_id=uuid.uuid4(),
        competitor_domain="noon.com",
        mode=ScrapeProfileMode.HTTP,
        canonical_url_hash=shared_hash,
    )
    second = ResolvedTarget(
        match_id=uuid.uuid4(),
        competitor_domain="noon.com",
        mode=ScrapeProfileMode.HTTP,
        canonical_url_hash=shared_hash,
    )
    assert len(coalesced_groups([first, second])) == 1


def test_targets_without_identity_are_never_coalesced_with_each_other() -> None:
    """W4.3's rule, kept: two unknowns are not a match.

    `None` in, singleton out — including against another `None`.
    """
    first = _target(canonical_url_hash=None)
    second = _target(canonical_url_hash=None)

    groups = coalesced_groups([first, second])

    assert len(groups) == 2
    assert all(not key.has_identity for key in groups)


def test_identity_less_targets_sort_after_identified_ones() -> None:
    identified = _target(canonical_url_hash="hash-a")
    anonymous = _target(canonical_url_hash=None)
    assert cluster_for_coalescing([anonymous, identified]) == [identified, anonymous]


# --- 4. cross-workspace sharing stays OFF -----------------------------------


def test_cross_workspace_input_is_refused_not_merged() -> None:
    """`CrossWorkspaceCoalescingUnsupported` remains the contract.

    A fetch shared between tenants has no answer to "whose budget paid for
    it", which is exactly why the cost-authorization service refuses the
    same thing. The audit's "up to 50 % duplicates" figure came from a
    duplicate-heavy pilot set and is not evidence for lifting this.
    """
    mine = _target(workspace_id=WORKSPACE)
    theirs = _target(workspace_id=uuid.uuid4())

    with pytest.raises(CrossWorkspaceCoalescingUnsupported):
        coalesced_groups([mine, theirs])


def test_same_workspace_input_is_not_refused() -> None:
    assert coalesced_groups([_target(), _target()])


def test_workspace_is_part_of_the_key() -> None:
    """Even if a future caller bypassed the guard, the key would not merge."""
    mine = _target(workspace_id=WORKSPACE)
    theirs = _target(workspace_id=uuid.uuid4())
    assert coalescing_key(mine) != coalescing_key(theirs)


# --- the key type itself -----------------------------------------------------


def test_key_field_order_is_the_documented_contract() -> None:
    """`cluster_for_coalescing` sorts on this order — it is not incidental."""
    assert CoalescingKey._FIELDS == (
        "workspace_id",
        "canonical_url_hash",
        "region",
        "currency",
        "proxy_country",
        "transport",
        "variant_selector_hash",
    )
    key = coalescing_key(
        _target(
            region="sa",
            currency="SAR",
            proxy_country="SA",
            transport="PROXY_HTTP",
            variant_selector_hash="v1",
        )
    )
    assert tuple(key) == (
        str(WORKSPACE),
        URL_HASH,
        "sa",
        "SAR",
        "SA",
        "PROXY_HTTP",
        "v1",
    )
    assert key.workspace_id == str(WORKSPACE)
    assert key.canonical_url_hash == URL_HASH
    assert key.has_identity is True
