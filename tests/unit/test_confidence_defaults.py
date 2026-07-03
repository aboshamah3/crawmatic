"""`app_shared/profiles/confidence.py` unit tests (SPEC-06 US4 T042, FR-011, SC-001).

Pure, DB-free. `DEFAULT_*` values match §17 exactly; `resolve_confidence_rules`
returns the defaults where a profile's bundle is unspecified/absent and the
profile's overrides where present; unknown keys are passed through (forward-
compat, per the contract).
"""

from __future__ import annotations

from app_shared.profiles.confidence import (
    DEFAULT_CONFIDENCE_RULES,
    DEFAULT_MIN_ACCEPTED_CONFIDENCE,
    DEFAULT_PROMOTION_THRESHOLD,
    resolve_confidence_rules,
)

# --- §17 verbatim constants ----------------------------------------------------


def test_default_confidence_rules_match_section_17_exactly() -> None:
    assert DEFAULT_CONFIDENCE_RULES == {
        "platform_variant_json": 0.95,
        "jsonld": 0.95,
        "embedded_json": 0.90,
        "css": 0.85,
        "xpath": 0.85,
        "regex": 0.75,
        "playwright": 0.80,
        "single_number": 0.40,
    }


def test_default_min_accepted_confidence_matches_section_17() -> None:
    assert DEFAULT_MIN_ACCEPTED_CONFIDENCE == 0.75


def test_default_promotion_threshold_matches_section_17() -> None:
    assert DEFAULT_PROMOTION_THRESHOLD == 0.85


# --- resolve_confidence_rules merge --------------------------------------------


def test_resolve_confidence_rules_none_returns_defaults_plus_thresholds() -> None:
    resolved = resolve_confidence_rules(None)
    for key, value in DEFAULT_CONFIDENCE_RULES.items():
        assert resolved[key] == value
    assert resolved["min_accepted_confidence"] == DEFAULT_MIN_ACCEPTED_CONFIDENCE
    assert resolved["promotion_threshold"] == DEFAULT_PROMOTION_THRESHOLD


def test_resolve_confidence_rules_empty_dict_returns_defaults() -> None:
    resolved = resolve_confidence_rules({})
    assert resolved["css"] == 0.85
    assert resolved["regex"] == 0.75


def test_resolve_confidence_rules_partial_override_replaces_only_given_keys() -> None:
    resolved = resolve_confidence_rules({"css": 0.60})
    assert resolved["css"] == 0.60
    # every other key keeps the documented default
    assert resolved["jsonld"] == 0.95
    assert resolved["embedded_json"] == 0.90
    assert resolved["xpath"] == 0.85
    assert resolved["regex"] == 0.75
    assert resolved["playwright"] == 0.80
    assert resolved["single_number"] == 0.40
    assert resolved["platform_variant_json"] == 0.95
    assert resolved["min_accepted_confidence"] == DEFAULT_MIN_ACCEPTED_CONFIDENCE
    assert resolved["promotion_threshold"] == DEFAULT_PROMOTION_THRESHOLD


def test_resolve_confidence_rules_overrides_min_accepted_and_promotion_threshold() -> None:
    resolved = resolve_confidence_rules(
        {"min_accepted_confidence": 0.80, "promotion_threshold": 0.90}
    )
    assert resolved["min_accepted_confidence"] == 0.80
    assert resolved["promotion_threshold"] == 0.90
    # unspecified per-method confidences still fall back to defaults
    assert resolved["css"] == 0.85


def test_resolve_confidence_rules_full_override_replaces_every_key() -> None:
    overrides = {key: 0.5 for key in DEFAULT_CONFIDENCE_RULES}
    resolved = resolve_confidence_rules(overrides)
    for key in DEFAULT_CONFIDENCE_RULES:
        assert resolved[key] == 0.5
    # thresholds untouched by the override still default
    assert resolved["min_accepted_confidence"] == DEFAULT_MIN_ACCEPTED_CONFIDENCE
    assert resolved["promotion_threshold"] == DEFAULT_PROMOTION_THRESHOLD


def test_resolve_confidence_rules_unknown_key_is_passed_through() -> None:
    resolved = resolve_confidence_rules({"future_method": 0.42})
    assert resolved["future_method"] == 0.42
    # known defaults are untouched by the unrelated unknown key
    assert resolved["css"] == 0.85


def test_resolve_confidence_rules_does_not_mutate_input_mapping() -> None:
    overrides = {"css": 0.60}
    original = dict(overrides)
    resolve_confidence_rules(overrides)
    assert overrides == original


def test_resolve_confidence_rules_does_not_mutate_module_level_defaults() -> None:
    original_defaults = dict(DEFAULT_CONFIDENCE_RULES)
    resolve_confidence_rules({"css": 0.10})
    assert DEFAULT_CONFIDENCE_RULES == original_defaults
