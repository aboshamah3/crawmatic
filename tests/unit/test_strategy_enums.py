"""SPEC-12 enum tests (FR-007/FR-008, data-model §1, D1/D2).

Pure membership/value assertions for the three new app-validated
``StrEnum``s plus the ``method_name`` cross-vocabulary validation gate
(``AccessMethod`` only valid when ``method_type=ACCESS``,
``ExtractionMethod`` only when ``EXTRACTION``) — no database, no infra.
"""

from __future__ import annotations

import pytest

from app_shared.enums import (
    AccessMethod,
    DiscoveryRunStatus,
    ExtractionMethod,
    MethodType,
    StrategyStatus,
    StrEnum,
    validate_method_name,
)


def test_strategy_status_members() -> None:
    assert issubclass(StrategyStatus, StrEnum)
    assert {member.value for member in StrategyStatus} == {
        "DISCOVERY_REQUIRED",
        "LEARNING",
        "ACTIVE",
        "DEGRADED",
        "DISABLED",
    }


def test_method_type_members() -> None:
    assert issubclass(MethodType, StrEnum)
    assert {member.value for member in MethodType} == {"ACCESS", "EXTRACTION"}


def test_discovery_run_status_members() -> None:
    assert issubclass(DiscoveryRunStatus, StrEnum)
    assert {member.value for member in DiscoveryRunStatus} == {
        "PENDING",
        "RUNNING",
        "COMPLETED",
        "NO_WINNER",
        "FAILED",
    }


@pytest.mark.parametrize("access_method", list(AccessMethod))
def test_validate_method_name_accepts_access_method_only_for_access_type(
    access_method: AccessMethod,
) -> None:
    assert validate_method_name(MethodType.ACCESS, access_method.value) == access_method.value
    with pytest.raises(ValueError):
        validate_method_name(MethodType.EXTRACTION, access_method.value)


@pytest.mark.parametrize("extraction_method", list(ExtractionMethod))
def test_validate_method_name_accepts_extraction_method_only_for_extraction_type(
    extraction_method: ExtractionMethod,
) -> None:
    assert (
        validate_method_name(MethodType.EXTRACTION, extraction_method.value)
        == extraction_method.value
    )
    with pytest.raises(ValueError):
        validate_method_name(MethodType.ACCESS, extraction_method.value)


def test_validate_method_name_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        validate_method_name(MethodType.ACCESS, "NOT_A_REAL_METHOD")
    with pytest.raises(ValueError):
        validate_method_name(MethodType.EXTRACTION, "NOT_A_REAL_METHOD")
