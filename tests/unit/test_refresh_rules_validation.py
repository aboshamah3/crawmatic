"""Refresh rule schema validation unit tests (SPEC-13 US1 T012, research R9).

Pure, DB-free: exercises `apps/api/app/schemas/refresh_rules.py` directly —
neither/both cadence -> `INVALID_CADENCE`; bad cron -> `INVALID_CRON`; each
scope's target-id matrix -> `SCOPE_TARGET_MISMATCH`; empty PATCH body ->
no fields set (the router raises `422 EMPTY_UPDATE` on this, mirrored here
by asserting `model_dump(exclude_unset=True)` is empty for a body-less
`RefreshRuleUpdate`). No FastAPI app, no database.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app_shared.enums import ScrapeScope
from app.schemas.refresh_rules import (
    RefreshRuleCreate,
    RefreshRuleUpdate,
    RefreshRuleValidationError,
    validate_cadence,
    validate_scope_target,
)

_WORKSPACE_TARGETS = {
    "product_id": None,
    "product_variant_id": None,
    "product_group_id": None,
    "competitor_id": None,
    "match_id": None,
}


def _targets(**overrides: uuid.UUID | None) -> dict[str, uuid.UUID | None]:
    merged = dict(_WORKSPACE_TARGETS)
    merged.update(overrides)
    return merged


# --- validate_cadence: exactly-one-of neither/both -> INVALID_CADENCE -------


def test_neither_cadence_field_is_invalid_cadence() -> None:
    with pytest.raises(RefreshRuleValidationError) as exc_info:
        validate_cadence(None, None)
    assert exc_info.value.code == "INVALID_CADENCE"


def test_both_cadence_fields_is_invalid_cadence() -> None:
    with pytest.raises(RefreshRuleValidationError) as exc_info:
        validate_cadence("*/5 * * * *", 5)
    assert exc_info.value.code == "INVALID_CADENCE"


def test_cron_only_is_valid_cadence() -> None:
    validate_cadence("*/5 * * * *", None)


def test_interval_only_is_valid_cadence() -> None:
    validate_cadence(None, 30)


# --- validate_cadence: unparseable cron -> INVALID_CRON ---------------------


def test_bad_cron_is_invalid_cron() -> None:
    with pytest.raises(RefreshRuleValidationError) as exc_info:
        validate_cadence("not a cron", None)
    assert exc_info.value.code == "INVALID_CRON"


def test_empty_cron_is_invalid_cron() -> None:
    with pytest.raises(RefreshRuleValidationError) as exc_info:
        validate_cadence("", None)
    assert exc_info.value.code == "INVALID_CRON"


# --- validate_scope_target: per-scope target-id matrix ----------------------


def test_workspace_scope_forbids_any_target_id() -> None:
    validate_scope_target(ScrapeScope.WORKSPACE, **_targets())

    with pytest.raises(RefreshRuleValidationError) as exc_info:
        validate_scope_target(
            ScrapeScope.WORKSPACE, **_targets(competitor_id=uuid.uuid4())
        )
    assert exc_info.value.code == "SCOPE_TARGET_MISMATCH"


@pytest.mark.parametrize(
    "scope,field_name",
    [
        (ScrapeScope.COMPETITOR, "competitor_id"),
        (ScrapeScope.PRODUCT, "product_id"),
        (ScrapeScope.VARIANT, "product_variant_id"),
        (ScrapeScope.PRODUCT_GROUP, "product_group_id"),
        (ScrapeScope.MATCH, "match_id"),
    ],
)
def test_non_workspace_scope_requires_exactly_its_own_target_id(
    scope: ScrapeScope, field_name: str
) -> None:
    # Missing entirely -> mismatch.
    with pytest.raises(RefreshRuleValidationError) as exc_info:
        validate_scope_target(scope, **_targets())
    assert exc_info.value.code == "SCOPE_TARGET_MISMATCH"

    # Exactly its own id -> valid.
    validate_scope_target(scope, **_targets(**{field_name: uuid.uuid4()}))

    # Its own id AND another field's id -> mismatch.
    other_field = next(f for f in _WORKSPACE_TARGETS if f != field_name)
    with pytest.raises(RefreshRuleValidationError) as exc_info:
        validate_scope_target(
            scope,
            **_targets(**{field_name: uuid.uuid4(), other_field: uuid.uuid4()}),
        )
    assert exc_info.value.code == "SCOPE_TARGET_MISMATCH"


# --- RefreshRuleCreate wires both validators in (model_validator) ----------


def test_refresh_rule_create_rejects_both_cadence_fields() -> None:
    with pytest.raises(ValidationError):
        RefreshRuleCreate(
            name="rule",
            scope=ScrapeScope.WORKSPACE,
            cron_expression="*/5 * * * *",
            interval_minutes=5,
        )


def test_refresh_rule_create_rejects_bad_cron() -> None:
    with pytest.raises(ValidationError):
        RefreshRuleCreate(name="rule", scope=ScrapeScope.WORKSPACE, cron_expression="garbage")


def test_refresh_rule_create_rejects_scope_target_mismatch() -> None:
    with pytest.raises(ValidationError):
        RefreshRuleCreate(
            name="rule",
            scope=ScrapeScope.PRODUCT,
            interval_minutes=30,
            # missing product_id
        )


def test_refresh_rule_create_accepts_valid_workspace_cron_rule() -> None:
    rule = RefreshRuleCreate(
        name="rule", scope=ScrapeScope.WORKSPACE, cron_expression="0 * * * *"
    )
    assert rule.enabled is True
    assert rule.priority == 0


def test_refresh_rule_create_accepts_valid_product_group_interval_rule() -> None:
    group_id = uuid.uuid4()
    rule = RefreshRuleCreate(
        name="rule",
        scope=ScrapeScope.PRODUCT_GROUP,
        product_group_id=group_id,
        interval_minutes=15,
    )
    assert rule.product_group_id == group_id


def test_refresh_rule_create_rejects_non_positive_interval() -> None:
    with pytest.raises(ValidationError):
        RefreshRuleCreate(name="rule", scope=ScrapeScope.WORKSPACE, interval_minutes=0)


def test_refresh_rule_create_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RefreshRuleCreate(
            name="rule",
            scope=ScrapeScope.WORKSPACE,
            interval_minutes=5,
            not_a_real_field=True,
        )


# --- RefreshRuleUpdate: empty PATCH body ------------------------------------


def test_empty_refresh_rule_update_has_no_fields_set() -> None:
    update = RefreshRuleUpdate()
    assert update.model_dump(exclude_unset=True) == {}


def test_refresh_rule_update_partial_body_only_sets_supplied_fields() -> None:
    update = RefreshRuleUpdate(enabled=False)
    assert update.model_dump(exclude_unset=True) == {"enabled": False}


def test_refresh_rule_update_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RefreshRuleUpdate(not_a_real_field=True)
