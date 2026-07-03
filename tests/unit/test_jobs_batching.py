"""Batch-planning unit tests (SPEC-08 T027, US1, FR-011, SC-008).

`app_shared.jobs.batching.plan_batches` — pure, no DB/Redis/network.
Per `contracts/batching.md`: single-domain/mode input forms one batch;
an over-`http_max` group splits into bounded chunks; `batch_index` is
stable across repeated calls; no match_id ever lands in two batches;
empty input yields no batches. Multi-domain/multi-mode grouping is
extended in US2 (T036) — this file covers only the single-domain shapes
in scope for US1.
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
