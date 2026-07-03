"""`app_shared/profiles/validation.py` unit tests (SPEC-06 US1 T017, FR-005/006/007, SC-006).

Pure, DB-free. Enum accept/reject; regex compile-ok / uncompilable /
catastrophic-pattern corpus; cookie technical-accept / session-auth-
reject corpus; and the **positive empty-extraction case** — a profile
with a valid mode/adapter but no selectors/xpath/regex is ACCEPTED, not
rejected (spec Edge Cases "all extraction fields empty").

The `validation_rules`/`confidence_rules`/money corpus (SPEC-06 US4
T043): `required_currency` 3-letter accept/reject; `min_price`/
`max_price` money finite + scale <= 4 + non-negative + `min <= max`
accept/reject; `reject_if_text_contains`/`prefer_text_contains`
list[str] accept / non-list reject; `confidence_rules` values in
`[0, 1]` accept / outside reject.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app_shared.profiles.validation import (
    ProfileValidationError,
    coerce_enums,
    compile_regex_or_reject,
    reject_session_cookies,
    validate_confidence_rules,
    validate_profile,
    validate_validation_rules,
)

# --- enum coercion (FR-005) ---------------------------------------------------


def test_coerce_enums_accepts_valid_values() -> None:
    coerce_enums({"mode": "HTTP", "adapter_key": "default_http", "variant_strategy": "PAGE_SINGLE_PRICE"})


def test_coerce_enums_accepts_absent_fields() -> None:
    coerce_enums({})


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "NOT_A_MODE"),
        ("adapter_key", "not_an_adapter"),
        ("variant_strategy", "NOT_A_STRATEGY"),
    ],
)
def test_coerce_enums_rejects_out_of_set_value(field: str, value: str) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        coerce_enums({field: value})
    assert exc_info.value.field == field
    assert exc_info.value.code == "INVALID_ENUM"


# --- regex compile + ReDoS heuristic (FR-006) ---------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        r"\$(\d+\.\d{2})",
        r"^[A-Z]{3}$",
        r"price:\s*(\d+)",
        r"[a-z0-9]+",
    ],
)
def test_benign_regex_compiles_and_passes(pattern: str) -> None:
    compile_regex_or_reject(pattern, field="price_regex")


def test_uncompilable_regex_is_rejected() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        compile_regex_or_reject(r"(unclosed", field="price_regex")
    assert exc_info.value.field == "price_regex"
    assert exc_info.value.code == "REGEX_UNCOMPILABLE"


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+",
        r"(a*)*",
        r"(a+)*",
        r"(a*)+",
        r"(a|a)+",
    ],
)
def test_catastrophic_backtracking_pattern_is_rejected(pattern: str) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        compile_regex_or_reject(pattern, field="price_regex")
    assert exc_info.value.field == "price_regex"
    assert exc_info.value.code == "REGEX_CATASTROPHIC"


def test_validate_profile_checks_every_regex_field() -> None:
    for field in ("price_regex", "old_price_regex", "currency_regex", "stock_regex"):
        with pytest.raises(ProfileValidationError) as exc_info:
            validate_profile({field: "(a+)+"})
        assert exc_info.value.field == field


# --- cookies (FR-007, §30) -----------------------------------------------------


@pytest.mark.parametrize(
    "cookies",
    [
        {"currency": "USD"},
        {"cur": "EUR"},
        {"lang": "en"},
        {"locale": "en-US"},
        {"country": "DE"},
        [{"name": "currency", "value": "USD"}, {"name": "locale", "value": "en"}],
    ],
)
def test_technical_cookies_are_accepted(cookies) -> None:
    reject_session_cookies(cookies)


@pytest.mark.parametrize(
    "name",
    [
        "session",
        "sessionid",
        "sid",
        "sess",
        "PHPSESSID",
        "JSESSIONID",
        "ASP.NET_SessionId",
        "connect.sid",
        "auth",
        "Authorization",
        "token",
        "access_token",
        "refresh_token",
        "jwt",
        "csrf",
        "xsrf",
        "remember",
        "remember_me",
        "login",
        "logged_in",
        "user",
        "uid",
        "account",
        "my_auth_cookie",
        "x-csrf-token",
    ],
)
def test_session_auth_cookie_names_are_rejected_dict_shape(name: str) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        reject_session_cookies({name: "value"})
    assert exc_info.value.field == "cookies"
    assert exc_info.value.code == "FORBIDDEN_COOKIE"


def test_session_auth_cookie_names_are_rejected_list_shape() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        reject_session_cookies([{"name": "sessionid", "value": "abc"}])
    assert exc_info.value.code == "FORBIDDEN_COOKIE"


def test_cookies_none_is_accepted() -> None:
    reject_session_cookies(None)


@pytest.mark.parametrize("bad_shape", ["not-a-dict-or-list", 42, [{"missing_name": "x"}], [1, 2]])
def test_malformed_cookies_shape_is_rejected(bad_shape) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        reject_session_cookies(bad_shape)
    assert exc_info.value.code == "INVALID_SHAPE"


# --- validation_rules / confidence_rules smoke (full corpus in T043) ---------


def test_validation_rules_valid_bundle_passes() -> None:
    validate_validation_rules(
        {"required_currency": "USD", "min_price": "1.00", "max_price": "999.99"}
    )


def test_validation_rules_none_passes() -> None:
    validate_validation_rules(None)


def test_confidence_rules_valid_bundle_passes() -> None:
    validate_confidence_rules({"css": 0.85, "regex": 0.75})


def test_confidence_rules_none_passes() -> None:
    validate_confidence_rules(None)


# --- validation_rules full corpus (SPEC-06 US4 T043, FR-008/FR-022) -----------


@pytest.mark.parametrize("currency", ["USD", "eur", "GbP", "JPY"])
def test_required_currency_accepts_3_letter_codes(currency: str) -> None:
    validate_validation_rules({"required_currency": currency})


@pytest.mark.parametrize(
    "currency",
    ["US", "USDD", "US1", "", "1SD", "US$", "usd1"],
)
def test_required_currency_rejects_non_3_letter_alpha_codes(currency: str) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"required_currency": currency})
    assert exc_info.value.field == "validation_rules.required_currency"
    assert exc_info.value.code == "INVALID_CURRENCY"


@pytest.mark.parametrize(
    "bundle",
    [
        {"min_price": "1.00"},
        {"max_price": "999.99"},
        {"min_price": "1.00", "max_price": "999.99"},
        {"min_price": "1.00", "max_price": "1.00"},  # min == max, boundary-ok
        {"min_price": Decimal("0.0001"), "max_price": Decimal("0.0002")},
        {"min_price": 0, "max_price": 10},
    ],
)
def test_min_max_price_accepts_finite_in_scale_non_negative_ordered_money(bundle: dict) -> None:
    validate_validation_rules(bundle)


def test_min_price_rejects_non_finite() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"min_price": Decimal("NaN")})
    assert exc_info.value.field == "validation_rules.min_price"
    assert exc_info.value.code == "INVALID_MONEY"


def test_max_price_rejects_infinity() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"max_price": Decimal("Infinity")})
    assert exc_info.value.field == "validation_rules.max_price"
    assert exc_info.value.code == "INVALID_MONEY"


def test_min_price_rejects_over_scale_not_rounded() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"min_price": Decimal("1.23456")})
    assert exc_info.value.field == "validation_rules.min_price"
    assert exc_info.value.code == "INVALID_MONEY"


def test_max_price_rejects_negative() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"max_price": Decimal("-1.00")})
    assert exc_info.value.field == "validation_rules.max_price"
    assert exc_info.value.code == "INVALID_MONEY"


def test_min_price_rejects_float_input() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"min_price": 1.5})
    assert exc_info.value.field == "validation_rules.min_price"
    assert exc_info.value.code == "INVALID_MONEY"


def test_min_gt_max_price_is_rejected() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"min_price": "10.00", "max_price": "1.00"})
    assert exc_info.value.field == "validation_rules"
    assert exc_info.value.code == "MIN_GT_MAX"


@pytest.mark.parametrize("field", ["reject_if_text_contains", "prefer_text_contains"])
@pytest.mark.parametrize(
    "value",
    [[], ["sold out"], ["sold out", "unavailable", "backorder"]],
    ids=["empty-list", "single", "multiple"],
)
def test_text_contains_fields_accept_list_of_str(field: str, value: list[str]) -> None:
    validate_validation_rules({field: value})


@pytest.mark.parametrize("field", ["reject_if_text_contains", "prefer_text_contains"])
@pytest.mark.parametrize(
    "bad_value",
    ["not-a-list", 42, {"a": "b"}, ["ok", 5], [None], [["nested"]]],
    ids=["string", "int", "dict", "mixed-int", "none-item", "nested-list"],
)
def test_text_contains_fields_reject_non_list_or_non_str_items(field: str, bad_value) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({field: bad_value})
    assert exc_info.value.field == f"validation_rules.{field}"
    assert exc_info.value.code == "INVALID_TEXT_LIST"


def test_validation_rules_full_bundle_accepted() -> None:
    validate_validation_rules(
        {
            "required_currency": "USD",
            "min_price": "1.00",
            "max_price": "999.99",
            "reject_if_text_contains": ["sold out"],
            "prefer_text_contains": ["in stock"],
        }
    )


def test_validation_rules_rejects_unknown_keys() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules({"unexpected_key": True})
    assert exc_info.value.field == "validation_rules"
    assert exc_info.value.code == "INVALID_SHAPE"


def test_validation_rules_rejects_non_dict_shape() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_validation_rules("not-a-dict")
    assert exc_info.value.field == "validation_rules"
    assert exc_info.value.code == "INVALID_SHAPE"


# --- confidence_rules full corpus (SPEC-06 US4 T043, FR-009) -------------------


@pytest.mark.parametrize(
    "bundle",
    [
        {"css": 0.0},
        {"css": 1.0},
        {"css": 0.5},
        {"css": 0.85, "regex": 0.75, "jsonld": 0.95},
        {},
    ],
)
def test_confidence_rules_accepts_values_in_unit_interval(bundle: dict) -> None:
    validate_confidence_rules(bundle)


@pytest.mark.parametrize(
    "value",
    [-0.01, 1.01, -1, 2, 100],
)
def test_confidence_rules_rejects_values_outside_unit_interval(value) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_confidence_rules({"css": value})
    assert exc_info.value.field == "confidence_rules.css"
    assert exc_info.value.code == "CONFIDENCE_OUT_OF_RANGE"


@pytest.mark.parametrize("value", ["not-a-number", None, [0.5], {"nested": 0.5}, True, False])
def test_confidence_rules_rejects_non_numeric_values(value) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_confidence_rules({"css": value})
    assert exc_info.value.field == "confidence_rules.css"
    assert exc_info.value.code == "INVALID_SHAPE"


def test_confidence_rules_rejects_non_dict_shape() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_confidence_rules("not-a-dict")
    assert exc_info.value.field == "confidence_rules"
    assert exc_info.value.code == "INVALID_SHAPE"


# --- positive empty-extraction case (spec Edge Cases) -------------------------


def test_profile_with_no_selectors_xpath_regex_is_accepted() -> None:
    """A profile with a valid mode/adapter but NO selectors/xpath/regex
    must be ACCEPTED, not rejected — later extraction may rely on
    JSON-LD/platform patterns alone."""
    validate_profile(
        {
            "mode": "HTTP",
            "adapter_key": "jsonld_first",
            "variant_strategy": "PAGE_SINGLE_PRICE",
            "price_selector": None,
            "price_xpath": None,
            "price_regex": None,
            "old_price_selector": None,
            "old_price_xpath": None,
            "old_price_regex": None,
            "currency_selector": None,
            "currency_xpath": None,
            "currency_regex": None,
            "stock_selector": None,
            "stock_xpath": None,
            "stock_regex": None,
            "title_selector": None,
            "title_xpath": None,
        }
    )


def test_profile_with_no_fields_at_all_is_accepted() -> None:
    validate_profile({})


def test_validate_profile_facade_raises_on_first_offending_field() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_profile({"mode": "NOT_A_MODE", "price_regex": "(a+)+"})
    assert exc_info.value.code == "INVALID_ENUM"
