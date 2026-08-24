"""Focused Phase-1 contracts for versioned profiles and method candidates."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.dialects import postgresql

from app_shared.enums import (
    AccessMethod,
    AdapterKey,
    ExtractionMethod,
    StrategyMethodProofState,
)
from app_shared.models.domain_playbooks import DomainPlaybook
from app_shared.models.observations import RequestAttempt
from app_shared.models.scrape_profiles import ScrapeProfile, ScrapeProfileRevision
from app_shared.models.strategy import (
    DomainStrategyMethod,
    DomainStrategyProfile,
    StrategyAttemptStats,
)
from app_shared.profiles.repository import visible_profile_revisions_select
from app_shared.profiles.revisioning import profile_snapshot
from app_shared.profiles.upsert import build_profiles_upsert


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _sql_with_binds(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _fk_names(table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def test_profile_has_version_adapter_config_and_immutable_revision_model() -> None:
    assert ScrapeProfile.__table__.c.version.nullable is False
    assert ScrapeProfile.__table__.c.version.default.arg == 1
    assert "JSONB" in str(ScrapeProfile.__table__.c.adapter_config.type)

    revision = ScrapeProfileRevision.__table__
    assert revision.c.workspace_id.nullable is True
    assert revision.c.snapshot.nullable is False
    assert "uq_scrape_profile_revisions_profile_version" in {
        constraint.name for constraint in revision.constraints
    }


def test_profile_snapshot_captures_generic_adapter_config_without_a_domain_branch() -> None:
    profile = ScrapeProfile(
        workspace_id=uuid.uuid4(),
        name="public-catalog",
        adapter_key=AdapterKey.DEFAULT_HTTP,
        version=7,
        adapter_config={
            "endpoint_template": "https://store.example/api/search?q={identifier}",
            "identifier_sources": ["competitor_variant_identifier", "url"],
            "exact_match": {"field": "sku"},
        },
    )

    snapshot = profile_snapshot(profile)

    assert snapshot["adapter_config"]["exact_match"] == {"field": "sku"}
    assert snapshot["adapter_key"] == AdapterKey.DEFAULT_HTTP.value
    assert "version" not in snapshot
    assert "workspace_id" not in snapshot


def test_bulk_upsert_increments_version_and_updates_adapter_config() -> None:
    statement = build_profiles_upsert(
        [
            {
                "workspace_id": uuid.uuid4(),
                "name": "catalog",
                "adapter_config": {"price_path": "/sale_price"},
            }
        ]
    )
    sql = _sql_with_binds(statement)
    assert "version = (scrape_profiles.version +" in sql
    assert "adapter_config = excluded.adapter_config" in sql


def test_revision_history_query_is_own_or_global_and_version_ordered() -> None:
    sql = _sql(visible_profile_revisions_select(uuid.uuid4(), uuid.uuid4()))
    assert "scrape_profile_revisions.workspace_id IS NULL" in sql
    assert "scrape_profile_revisions.version DESC" in sql


def test_domain_strategy_method_retains_both_axes_and_version_history_fields() -> None:
    table = DomainStrategyMethod.__table__
    expected = {
        "workspace_id",
        "domain_strategy_profile_id",
        "scrape_profile_id",
        "scrape_profile_version",
        "access_method",
        "extraction_method",
        "priority",
        "method_version",
        "enter_on",
        "fallback_on",
        "enabled",
        "proof_state",
        "cooldown_until",
        "next_canary_at",
        "proof_sample_size",
        "circuit_attempt_count",
        "circuit_failure_count",
        "consecutive_failure_count",
        "supersedes_method_id",
        "retired_at",
    }
    assert expected.issubset(table.c.keys())
    assert table.c.proof_state.default.arg == StrategyMethodProofState.CANDIDATE
    assert table.c.access_method.type.length == 32
    assert table.c.extraction_method.type.length == 32


def test_preferred_pointer_and_stats_supplement_legacy_fields() -> None:
    profile = DomainStrategyProfile.__table__
    assert "preferred_access_method" in profile.c
    assert "preferred_extraction_method" in profile.c
    assert "preferred_method_id" in profile.c
    assert "fk_dsp_preferred_method_domain_strategy_methods" in _fk_names(profile)

    stats = StrategyAttemptStats.__table__
    assert "method_type" in stats.c
    assert "method_name" in stats.c
    assert "strategy_method_id" in stats.c


def test_request_attempt_audits_exact_method_revision_identity_and_terminality() -> None:
    table = RequestAttempt.__table__
    expected = {
        "strategy_method_id",
        "scrape_profile_id",
        "scrape_profile_version",
        "adapter_key",
        "final_url",
        "identity_validation_result",
        "terminal_for_target",
    }
    assert expected.issubset(table.c.keys())
    assert table.c.terminal_for_target.default.arg is True
    assert {
        "fk_request_attempts_strategy_method_id_domain_strategy_methods",
        "fk_request_attempts_scrape_profile_id_scrape_profiles",
    }.issubset(_fk_names(table))


def test_playwright_direct_is_distinct_from_proxied_browser() -> None:
    assert AccessMethod.PLAYWRIGHT_DIRECT != AccessMethod.PLAYWRIGHT_PROXY
    assert AccessMethod.PLAYWRIGHT_DIRECT.value == "PLAYWRIGHT_DIRECT"


def test_domain_playbook_holds_generic_ordered_method_templates() -> None:
    column = DomainPlaybook.__table__.c.method_templates
    assert column.nullable is False
    assert callable(column.default.arg)


def test_candidate_can_represent_unrelated_workspaces_products_and_competitors() -> None:
    method = DomainStrategyMethod(
        workspace_id=uuid.uuid4(),
        domain_strategy_profile_id=uuid.uuid4(),
        scrape_profile_id=uuid.uuid4(),
        scrape_profile_version=3,
        access_method=AccessMethod.DIRECT_HTTP_RETRY,
        extraction_method=ExtractionMethod.PLATFORM_JSON,
        priority=2,
        fallback_on=["TIMEOUT", "HTTP_403"],
        proof_state=StrategyMethodProofState.PROVEN,
        cooldown_until=datetime.now(timezone.utc),
    )
    assert method.fallback_on == ["TIMEOUT", "HTTP_403"]
    assert not hasattr(method, "product_name")
