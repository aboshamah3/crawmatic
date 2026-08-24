from __future__ import annotations

import uuid

from app_shared.enums import AccessMethod, ExtractionMethod, StrategyMethodProofState, StrategyStatus
from app_shared.models.strategy import DomainStrategyMethod, DomainStrategyProfile
from app_shared.strategy.repository import seed_versioned_method_from_discovery


class _Rows:
    def __init__(self, values: list[DomainStrategyMethod]) -> None:
        self.values = values

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[DomainStrategyMethod]:
        return self.values


class _Session:
    def __init__(self, rows: list[DomainStrategyMethod] | None = None) -> None:
        self.rows = rows or []

    def execute(self, _statement: object) -> _Rows:
        return _Rows(self.rows)

    def add(self, row: DomainStrategyMethod) -> None:
        row.id = uuid.uuid4()
        self.rows.append(row)

    def flush(self) -> None:
        return None


def test_discovery_materializes_a_method_without_a_curated_domain_playbook() -> None:
    profile = DomainStrategyProfile(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain="new-competitor-for-an-unrelated-product.example",
        url_pattern="new-competitor-for-an-unrelated-product.example",
        url_pattern_version=1,
        status=StrategyStatus.ACTIVE,
    )
    session = _Session()

    method = seed_versioned_method_from_discovery(
        session,
        profile=profile,
        winning_access=AccessMethod.DIRECT_HTTP_RETRY,
        winning_extraction=ExtractionMethod.JSON_LD,
        proof_sample_size=4,
    )

    assert method.scrape_profile_id is None
    assert method.proof_state is StrategyMethodProofState.PROVEN
    assert method.proof_sample_size == 4
    assert profile.preferred_method_id == method.id


def test_repeat_discovery_reuses_the_retained_combined_method() -> None:
    profile = DomainStrategyProfile(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain="another.example",
        url_pattern="another.example",
        url_pattern_version=1,
        status=StrategyStatus.ACTIVE,
    )
    existing = DomainStrategyMethod(
        id=uuid.uuid4(),
        workspace_id=profile.workspace_id,
        domain_strategy_profile_id=profile.id,
        access_method=AccessMethod.DIRECT_HTTP,
        extraction_method=ExtractionMethod.CSS,
        priority=0,
        proof_state=StrategyMethodProofState.CANDIDATE,
        proof_sample_size=2,
    )
    session = _Session([existing])

    selected = seed_versioned_method_from_discovery(
        session,
        profile=profile,
        winning_access=AccessMethod.DIRECT_HTTP,
        winning_extraction=ExtractionMethod.CSS,
        proof_sample_size=5,
    )

    assert selected is existing
    assert len(session.rows) == 1
    assert selected.proof_state is StrategyMethodProofState.PROVEN
    assert selected.proof_sample_size == 5
