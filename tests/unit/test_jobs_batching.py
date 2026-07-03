"""Batch-planning unit tests (SPEC-08 T027/T036, US1/US2, FR-011, SC-008).

`app_shared.jobs.batching.plan_batches` — pure, no DB/Redis/network.
Per `contracts/batching.md`: single-domain/mode input forms one batch;
an over-`http_max` group splits into bounded chunks; `batch_index` is
stable across repeated calls; no match_id ever lands in two batches;
empty input yields no batches. Multi-domain/multi-mode targets group
by `(domain, mode)` into one Scrapyd batch per group — not one per
match/URL (SC-008) — each group's chunks honoring the 50-200 HTTP
guidance (US2, T036).
"""

from __future__ import annotations

import uuid

from app_shared.enums import ScrapeProfileMode
from app_shared.jobs.batching import Batch, ResolvedTarget, plan_batches


def _targets(n: int, *, domain: str = "shop.example.com", mode: ScrapeProfileMode = ScrapeProfileMode.HTTP) -> list[ResolvedTarget]:
    return [
        ResolvedTarget(match_id=uuid.uuid4(), competitor_domain=domain, mode=mode)
        for _ in range(n)
    ]


def test_empty_input_yields_no_batches() -> None:
    assert plan_batches([]) == []


def test_single_target_single_domain_forms_one_batch_at_index_zero() -> None:
    targets = _targets(1)

    batches = plan_batches(targets)

    assert len(batches) == 1
    batch = batches[0]
    assert isinstance(batch, Batch)
    assert batch.batch_index == 0
    assert batch.domain == "shop.example.com"
    assert batch.mode == ScrapeProfileMode.HTTP
    assert batch.match_ids == [targets[0].match_id]


def test_small_group_forms_a_single_batch() -> None:
    targets = _targets(5)

    batches = plan_batches(targets, http_min=50, http_max=200)

    assert len(batches) == 1
    assert len(batches[0].match_ids) == 5


def test_over_http_max_group_splits_into_bounded_chunks() -> None:
    targets = _targets(450)

    batches = plan_batches(targets, http_min=50, http_max=200)

    assert len(batches) == 3
    sizes = [len(batch.match_ids) for batch in batches]
    assert sizes == [200, 200, 50]
    for size in sizes:
        assert 1 <= size <= 200


def test_no_match_id_appears_in_two_batches() -> None:
    targets = _targets(450)

    batches = plan_batches(targets, http_max=200)

    seen: set[uuid.UUID] = set()
    for batch in batches:
        for match_id in batch.match_ids:
            assert match_id not in seen, "match_id duplicated across batches"
            seen.add(match_id)

    assert seen == {target.match_id for target in targets}


def test_batch_index_is_stable_across_repeated_calls() -> None:
    targets = _targets(450)

    first = plan_batches(targets, http_max=200)
    second = plan_batches(targets, http_max=200)

    assert [batch.batch_index for batch in first] == [batch.batch_index for batch in second]
    assert [batch.match_ids for batch in first] == [batch.match_ids for batch in second]


def test_batch_indices_are_sequential_starting_at_zero() -> None:
    targets = _targets(450)

    batches = plan_batches(targets, http_max=200)

    assert [batch.batch_index for batch in batches] == list(range(len(batches)))


# --- multi-domain / multi-mode grouping (US2, T036) --------------------------


def test_multi_domain_targets_group_into_one_batch_per_domain() -> None:
    targets = (
        _targets(5, domain="a.example.com")
        + _targets(3, domain="b.example.com")
        + _targets(4, domain="c.example.com")
    )

    batches = plan_batches(targets, http_min=50, http_max=200)

    # One Scrapyd batch per (domain, mode) group -- not one per match/URL.
    assert len(batches) == 3
    domains = {batch.domain for batch in batches}
    assert domains == {"a.example.com", "b.example.com", "c.example.com"}
    for batch in batches:
        assert 1 <= len(batch.match_ids) <= 200


def test_multi_mode_targets_for_same_domain_form_separate_batches() -> None:
    targets = _targets(5, domain="shop.example.com", mode=ScrapeProfileMode.HTTP) + _targets(
        5, domain="shop.example.com", mode=ScrapeProfileMode.BROWSER
    )

    batches = plan_batches(targets, http_min=50, http_max=200)

    # Same domain, two modes -> two distinct batches (a batch always
    # carries exactly one domain + one mode).
    assert len(batches) == 2
    modes = {batch.mode for batch in batches}
    assert modes == {ScrapeProfileMode.HTTP, ScrapeProfileMode.BROWSER}
    for batch in batches:
        assert batch.domain == "shop.example.com"
        assert len(batch.match_ids) == 5


def test_multi_domain_multi_mode_targets_group_by_domain_and_mode_pair() -> None:
    targets = (
        _targets(60, domain="a.example.com", mode=ScrapeProfileMode.HTTP)
        + _targets(30, domain="a.example.com", mode=ScrapeProfileMode.BROWSER)
        + _targets(250, domain="b.example.com", mode=ScrapeProfileMode.HTTP)
    )

    batches = plan_batches(targets, http_min=50, http_max=200)

    # a/HTTP -> 1 batch, a/BROWSER -> 1 batch, b/HTTP (250) -> 2 chunks (200 + 50).
    assert len(batches) == 4
    seen: set[uuid.UUID] = set()
    for batch in batches:
        assert batch.domain in {"a.example.com", "b.example.com"}
        assert 1 <= len(batch.match_ids) <= 200
        for match_id in batch.match_ids:
            assert match_id not in seen, "match_id duplicated across batches"
            seen.add(match_id)
    assert seen == {target.match_id for target in targets}

    # batch_index tracks the canonical (domain, mode) sort, then chunk
    # order within the group -- stable across a repeated call.
    again = plan_batches(targets, http_min=50, http_max=200)
    assert [b.batch_index for b in batches] == [b.batch_index for b in again]
    assert [b.domain for b in batches] == [b.domain for b in again]
    assert [b.mode for b in batches] == [b.mode for b in again]
