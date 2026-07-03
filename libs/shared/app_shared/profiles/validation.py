"""Pure, framework-agnostic profile validators (`contracts/profile-validation.md`, SPEC-06 US1).

Every rejection raises :class:`ProfileValidationError` (``field``, ``code``,
``message``) — the router maps it to ``422
{error:{code:"VALIDATION_ERROR", field, message}}``; the bulk-upsert path
(``app_shared.profiles.upsert.prepare_profiles``) collects it per-row into
``rejected[]`` instead of aborting the batch (FR-020).

Reuses ``app_shared.money.parse_money`` (the extracted pure §19 money
boundary) for ``validation_rules.min_price``/``max_price`` so there is
exactly one implementation of the finite/scale/non-negative money check
(Principle VII). No DB, no FastAPI, no I/O — safe to call from
``apps/api`` routers, the bulk-upsert core, and unit tests alike.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app_shared.enums import AdapterKey, ScrapeProfileMode, VariantStrategy
from app_shared.money import parse_money

# --- error type --------------------------------------------------------------


class ProfileValidationError(ValueError):
    """A structured, field-specific profile-validation rejection (SC-006).

    ``code`` is one of: ``INVALID_ENUM``, ``REGEX_UNCOMPILABLE``,
    ``REGEX_CATASTROPHIC``, ``FORBIDDEN_COOKIE``, ``INVALID_CURRENCY``,
    ``INVALID_MONEY``, ``MIN_GT_MAX``, ``INVALID_TEXT_LIST``,
    ``CONFIDENCE_OUT_OF_RANGE``, ``INVALID_SHAPE``.
    """

    def __init__(self, field: str, code: str, message: str) -> None:
        self.field = field
        self.code = code
        self.message = message
        super().__init__(f"{field}: {message} [{code}]")


# --- enum coercion (FR-005) ---------------------------------------------------

_ENUM_FIELDS: dict[str, type] = {
    "mode": ScrapeProfileMode,
    "adapter_key": AdapterKey,
    "variant_strategy": VariantStrategy,
}


def coerce_enums(payload: Mapping[str, Any]) -> None:
    """Validate every present enum field (``mode``/``adapter_key``/``variant_strategy``).

    A field absent from ``payload`` (or explicitly ``None``) is skipped —
    the ORM/schema default applies. An out-of-set value raises
    ``ProfileValidationError(field, "INVALID_ENUM", ...)`` (FR-005).
    """
    for field, enum_type in _ENUM_FIELDS.items():
        if field not in payload or payload[field] is None:
            continue
        value = payload[field]
        if isinstance(value, enum_type):
            continue
        try:
            enum_type(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in enum_type)
            raise ProfileValidationError(
                field,
                "INVALID_ENUM",
                f"{value!r} is not a valid {enum_type.__name__} value "
                f"(expected one of: {valid})",
            ) from exc


# --- regex compile + catastrophic-backtracking heuristic (FR-006) ------------

_REGEX_FIELDS: tuple[str, ...] = (
    "price_regex",
    "old_price_regex",
    "currency_regex",
    "stock_regex",
)

# Heuristic (documented, not a safety proof — see contracts/profile-validation.md):
# reject a group containing an inner unbounded quantifier (+/*/{n,}) that is
# itself unbounded-quantified ((X+)+, (X*)*, (X+)*, (X*)+, and the {n,}
# variants of either side), and overlapping alternation under +/* where both
# branches are textually identical (e.g. (a|a)+).
_NESTED_QUANTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\([^()]*[+*][^()]*\)[+*]"),
    re.compile(r"\([^()]*\{\d+,\}[^()]*\)[+*]"),
    re.compile(r"\([^()]*[+*][^()]*\)\{\d+,\}"),
)
_OVERLAPPING_ALTERNATION = re.compile(r"\(([^()|]+)\|([^()|]+)\)[+*]")


def _catastrophic_backtracking_risk(pattern: str) -> bool:
    if any(rx.search(pattern) for rx in _NESTED_QUANTIFIER_PATTERNS):
        return True
    match = _OVERLAPPING_ALTERNATION.search(pattern)
    if match and match.group(1) == match.group(2):
        return True
    return False


def compile_regex_or_reject(pattern: str, *, field: str) -> None:
    """``re.compile(pattern)`` + the catastrophic-backtracking heuristic (FR-006).

    An uncompilable pattern raises ``REGEX_UNCOMPILABLE``; a pattern
    matching the heuristic screen raises ``REGEX_CATASTROPHIC`` (a
    best-effort screen, not a formal safety proof).
    """
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ProfileValidationError(
            field, "REGEX_UNCOMPILABLE", f"{pattern!r} does not compile: {exc}"
        ) from exc
    if _catastrophic_backtracking_risk(pattern):
        raise ProfileValidationError(
            field,
            "REGEX_CATASTROPHIC",
            f"{pattern!r} matches the catastrophic-backtracking heuristic screen",
        )


def _validate_regex_fields(payload: Mapping[str, Any]) -> None:
    for field in _REGEX_FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ProfileValidationError(field, "INVALID_SHAPE", f"{field} must be a string")
        compile_regex_or_reject(value, field=field)


# --- cookie session/auth deny (FR-007, §30) -----------------------------------

_DENY_COOKIE_NAMES: frozenset[str] = frozenset(
    {
        "session",
        "sessionid",
        "sid",
        "sess",
        "phpsessid",
        "jsessionid",
        "asp.net_sessionid",
        "connect.sid",
        "auth",
        "authorization",
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
    }
)
_DENY_COOKIE_SUBSTRINGS: tuple[str, ...] = (
    "session",
    "auth",
    "token",
    "sid",
    "csrf",
    "xsrf",
    "login",
)


def _cookie_names(cookies: Any) -> list[str]:
    if isinstance(cookies, dict):
        return list(cookies.keys())
    if isinstance(cookies, list):
        names: list[str] = []
        for item in cookies:
            if not isinstance(item, dict) or "name" not in item:
                raise ProfileValidationError(
                    "cookies",
                    "INVALID_SHAPE",
                    "each list cookie entry must be an object with a 'name' key",
                )
            names.append(item["name"])
        return names
    raise ProfileValidationError(
        "cookies",
        "INVALID_SHAPE",
        "cookies must be a {name: value} object or a list of {name, value} objects",
    )


def reject_session_cookies(cookies: Any) -> None:
    """Reject any cookie whose name matches the auth/session deny heuristic (FR-007, §30).

    ``cookies`` may be a ``{name: value}`` dict or a list of
    ``{name, value}`` objects. Technical cookies (currency/locale, e.g.
    ``currency``/``cur``/``lang``/``locale``/``country``) are accepted.
    """
    if cookies is None:
        return
    for name in _cookie_names(cookies):
        if not isinstance(name, str):
            raise ProfileValidationError(
                "cookies", "INVALID_SHAPE", f"cookie name must be a string, got {name!r}"
            )
        lower = name.lower()
        if lower in _DENY_COOKIE_NAMES or any(sub in lower for sub in _DENY_COOKIE_SUBSTRINGS):
            raise ProfileValidationError(
                "cookies",
                "FORBIDDEN_COOKIE",
                f"cookie name {name!r} looks like a session/authentication cookie",
            )


# --- validation_rules (§18/§19, FR-008/FR-022) --------------------------------

_VALIDATION_RULES_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "required_currency",
        "min_price",
        "max_price",
        "reject_if_text_contains",
        "prefer_text_contains",
    }
)
_TEXT_LIST_FIELDS: tuple[str, ...] = ("reject_if_text_contains", "prefer_text_contains")


def validate_validation_rules(bundle: Any) -> None:
    """Shape/semantic-validate the ``validation_rules`` JSONB bundle (FR-008/FR-022).

    - ``required_currency`` (if present): a 3-letter alphabetic code.
    - ``min_price``/``max_price`` (if present): via
      ``app_shared.money.parse_money`` (finite, scale <= 4, non-negative);
      if both present, ``min_price <= max_price``.
    - ``reject_if_text_contains``/``prefer_text_contains`` (if present):
      ``list[str]``.
    - Unknown keys are rejected (strictness at write).
    """
    if bundle is None:
        return
    if not isinstance(bundle, dict):
        raise ProfileValidationError(
            "validation_rules", "INVALID_SHAPE", "validation_rules must be an object"
        )

    unknown = set(bundle.keys()) - _VALIDATION_RULES_KNOWN_KEYS
    if unknown:
        raise ProfileValidationError(
            "validation_rules",
            "INVALID_SHAPE",
            f"unknown validation_rules keys: {sorted(unknown)}",
        )

    currency = bundle.get("required_currency")
    if currency is not None:
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise ProfileValidationError(
                "validation_rules.required_currency",
                "INVALID_CURRENCY",
                f"{currency!r} is not a 3-letter currency code",
            )

    min_decimal = None
    max_decimal = None
    min_price = bundle.get("min_price")
    if min_price is not None:
        try:
            min_decimal = parse_money(min_price, non_negative=True)
        except (TypeError, ValueError) as exc:
            raise ProfileValidationError(
                "validation_rules.min_price", "INVALID_MONEY", str(exc)
            ) from exc
    max_price = bundle.get("max_price")
    if max_price is not None:
        try:
            max_decimal = parse_money(max_price, non_negative=True)
        except (TypeError, ValueError) as exc:
            raise ProfileValidationError(
                "validation_rules.max_price", "INVALID_MONEY", str(exc)
            ) from exc
    if min_decimal is not None and max_decimal is not None and min_decimal > max_decimal:
        raise ProfileValidationError(
            "validation_rules",
            "MIN_GT_MAX",
            f"min_price {min_decimal} must be <= max_price {max_decimal}",
        )

    for field in _TEXT_LIST_FIELDS:
        value = bundle.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ProfileValidationError(
                f"validation_rules.{field}",
                "INVALID_TEXT_LIST",
                f"{field} must be a list of strings",
            )


# --- confidence_rules (§17, FR-009) -------------------------------------------


def validate_confidence_rules(bundle: Any) -> None:
    """Every present numeric value in ``confidence_rules`` must be a real number in ``[0, 1]``."""
    if bundle is None:
        return
    if not isinstance(bundle, dict):
        raise ProfileValidationError(
            "confidence_rules", "INVALID_SHAPE", "confidence_rules must be an object"
        )
    for key, value in bundle.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProfileValidationError(
                f"confidence_rules.{key}",
                "INVALID_SHAPE",
                f"confidence_rules.{key} must be a number",
            )
        if not (0 <= value <= 1):
            raise ProfileValidationError(
                f"confidence_rules.{key}",
                "CONFIDENCE_OUT_OF_RANGE",
                f"confidence_rules.{key} = {value} is outside [0, 1]",
            )


# --- facade --------------------------------------------------------------


def validate_profile(payload: Mapping[str, Any]) -> None:
    """Run every write-time validator against ``payload`` (dict-shaped).

    Raises the first offending ``ProfileValidationError``: enum coercion
    -> every non-null ``*_regex`` compile/screen -> cookie deny ->
    ``validation_rules`` -> ``confidence_rules``. Used by the router
    (single create/update) and ``app_shared.profiles.upsert.prepare_profiles``
    (bulk, per row -> ``rejected[]``).

    A profile with a valid mode/adapter but **no** selectors/xpath/regex
    is accepted, not rejected (spec Edge Case "all extraction fields
    empty") — every check here is opt-in per present field.
    """
    coerce_enums(payload)
    _validate_regex_fields(payload)
    reject_session_cookies(payload.get("cookies"))
    validate_validation_rules(payload.get("validation_rules"))
    validate_confidence_rules(payload.get("confidence_rules"))
