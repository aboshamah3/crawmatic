# Contract: profile validators (`app_shared/profiles/validation.py`, pure)

Framework-agnostic, DB-free. Every rejection raises `ProfileValidationError(field, code, message)` — the router maps it to `422 {error:{code:"VALIDATION_ERROR", field, message}}`; the bulk path collects it into `rejected[]`.

## `ProfileValidationError(field: str, code: str, message: str)`

Structured, field-specific (SC-006). `code` ∈ {`INVALID_ENUM`, `REGEX_UNCOMPILABLE`, `REGEX_CATASTROPHIC`, `FORBIDDEN_COOKIE`, `INVALID_CURRENCY`, `INVALID_MONEY`, `MIN_GT_MAX`, `INVALID_TEXT_LIST`, `CONFIDENCE_OUT_OF_RANGE`, `INVALID_SHAPE`}.

## Enum coercion

`mode`/`adapter_key`/`variant_strategy` coerced via `ScrapeProfileMode`/`AdapterKey`/`VariantStrategy`; an out-of-set value → `INVALID_ENUM` (FR-005).

## `compile_regex_or_reject(pattern: str, *, field: str) -> None`

- `re.compile(pattern)` — failure → `REGEX_UNCOMPILABLE` (FR-006).
- Heuristic catastrophic-backtracking screen → `REGEX_CATASTROPHIC` (FR-006, best-effort): reject nested unbounded quantifiers on a group (`(…+)+`, `(…*)*`, `(…+)*`, `(…*)+`), a quantified group whose body contains an inner unbounded quantifier, and overlapping alternation under `+`/`*` (e.g. `(a|a)+`). Documented as a heuristic, not a safety proof.
- Applied to every non-null `*_regex` (`price_regex`, `old_price_regex`, `currency_regex`, `stock_regex`).

## `reject_session_cookies(cookies) -> None`

Reject any cookie whose **name** matches the auth/session deny heuristic (FR-007, §30): explicit deny-list (`session`, `sessionid`, `sid`, `sess`, `phpsessid`, `jsessionid`, `asp.net_sessionid`, `connect.sid`, `auth`, `authorization`, `token`, `access_token`, `refresh_token`, `jwt`, `csrf`, `xsrf`, `remember`, `remember_me`, `login`, `logged_in`, `user`, `uid`, `account`) **plus** a case-insensitive substring screen (`session`, `auth`, `token`, `sid`, `csrf`, `xsrf`, `login`) → `FORBIDDEN_COOKIE`. Technical cookies (currency/locale, e.g. `currency`, `cur`, `lang`, `locale`, `country`) are accepted. Accepts `cookies` as a dict `{name: value}` or a list of `{name, value}` (shape validated → `INVALID_SHAPE`).

## `validate_validation_rules(bundle) -> None` (§18/§19, FR-008/FR-022)

- `required_currency` (if present): a 3-letter alphabetic code (case-insensitive, stored uppercased) → else `INVALID_CURRENCY`.
- `min_price`/`max_price` (if present): via `app_shared.money.parse_money` → Decimal, finite (reject NaN/Infinity), scale ≤ 4 (reject over-scale, never round), **non-negative** → else `INVALID_MONEY`. If both present, `min_price ≤ max_price` → else `MIN_GT_MAX`.
- `reject_if_text_contains`/`prefer_text_contains` (if present): lists of strings → else `INVALID_TEXT_LIST`.
- Unknown keys tolerated (opaque forward-compat) or rejected — choose reject-unknown for strictness at write, documented in the module.

## `validate_confidence_rules(bundle) -> None` (§17, FR-009)

Every present numeric value (per-method confidences, minimum-accepted, promotion-threshold) is a real number in `[0,1]` → else `CONFIDENCE_OUT_OF_RANGE`. Non-numeric → `INVALID_SHAPE`.

## `validate_profile(payload) -> None` (facade)

Runs enum coercion + every non-null `*_regex` compile/screen + cookie deny + `validation_rules` + `confidence_rules`. Raises on the first offending field. Used by the router (single/create/update) and `prepare_profiles` (bulk, per row → `rejected[]`).

## Money reuse

`app_shared/money.py` gains a pure `parse_money(value) -> Decimal` (the existing `Money.process_bind_param` finite/scale/no-float logic + a non-negative option) called by both `Money` and this validator, so §19 has one implementation (Principle VII).

## Tests (unit, no DB)

- Enum accept/reject corpus per field.
- Regex: valid compiles pass; un-compilable rejected; classic catastrophic shapes rejected; benign patterns pass.
- Cookies: technical accepted; each deny-listed/substring-matched name rejected (dict and list shapes).
- `validation_rules`: valid bundle passes; bad currency, NaN/Infinity, >4dp, negative, min>max, non-list text each rejected.
- `confidence_rules`: values in [0,1] pass; <0 / >1 / non-numeric rejected.
- `parse_money`: float rejected, NaN/Infinity rejected, over-scale rejected (not rounded), negative rejected, valid Decimal/int/str accepted.
