from __future__ import annotations

import uuid

from app_shared.enums import AccessMethod, StrategyMethodProofState, StrategyStatus
from app_shared.models.domain_playbooks import DomainPlaybook
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.models.strategy import DomainStrategyMethod, DomainStrategyProfile
from app_shared.strategy.resolution import _materialize_playbook_methods


class _Rows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[object]:
        return self._values


class _Session:
    def __init__(self, profiles: list[ScrapeProfile]) -> None:
        self.profiles = profiles
        self.added: list[DomainStrategyMethod] = []

    def execute(self, _statement: object) -> _Rows:
        return _Rows(list(self.profiles))

    def add_all(self, values: list[DomainStrategyMethod]) -> None:
        self.added.extend(values)
        for value in values:
            if value.id is None:
                value.id = uuid.uuid4()

    def flush(self) -> None:
        return None


def test_materializes_generic_ordered_branches_for_an_unrelated_domain() -> None:
    workspace_id = uuid.uuid4()
    reusable = ScrapeProfile(
        id=uuid.uuid4(),
        workspace_id=None,
        name="generic.catalog.v7",
        version=7,
    )
    playbook = DomainPlaybook(
        domain="competitor-for-a-future-customer.example",
        preferred_access_method=AccessMethod.DIRECT_HTTP,
        method_templates=[
            {
                "priority": 0,
                "access_method": "DIRECT_HTTP",
                "scrape_profile_name": reusable.name,
                "proof_state": "PROVEN",
                "fallback_on": ["HTTP_404", "TIMEOUT"],
            },
            {
                "priority": 1,
                "access_method": "PLAYWRIGHT_DIRECT",
                "enter_on": ["TIMEOUT"],
            },
        ],
    )
    profile = DomainStrategyProfile(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        competitor_id=uuid.uuid4(),
        domain=playbook.domain,
        url_pattern=playbook.domain,
        url_pattern_version=1,
        status=StrategyStatus.LEARNING,
    )
    session = _Session([reusable])

    _materialize_playbook_methods(session, profile=profile, playbook=playbook)

    assert [row.priority for row in session.added] == [0, 1]
    assert session.added[0].scrape_profile_id == reusable.id
    assert session.added[0].scrape_profile_version == 7
    assert session.added[1].enter_on == ["TIMEOUT"]
    assert session.added[0].proof_state is StrategyMethodProofState.PROVEN
    assert profile.preferred_method_id == session.added[0].id
    assert profile.preferred_access_method is AccessMethod.DIRECT_HTTP
