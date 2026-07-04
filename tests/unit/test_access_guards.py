"""Cross-cutting security guards for SPEC-10 (T037).

Three independent, infra-free static/AST assertions:

1. No `/v1` response schema for the access-config API (`proxy_providers`,
   `access_policies`, `domain_access_rules`) can carry a `password`,
   `password_encrypted`, or `password_key_version` field (SC-003) — only
   the derived boolean `has_password` is allowed on `ProxyProviderResponse`.
2. `generic_price_spider.py` never logs a decrypted proxy password: every
   `logger.<level>(...)` call site in the module is AST-walked and none of
   its arguments may reference a variable/attribute known to hold a
   decrypted secret (`password`, `provider_passwords`,
   `_provider_passwords`, `token` built from `username:password`).
3. `app_shared/access/budget.py` contains no `request_attempts` reference
   anywhere in its source (FR-010, §22 — the budget/ceiling/cooldown
   counters are pure Redis `INCR`/`EXPIRE`/`SET NX EX` math, never a scan
   of the attempts table).
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

FORBIDDEN_PASSWORD_FIELDS = {"password", "password_encrypted", "password_key_version"}


# --- 1. response schemas never carry password/ciphertext ---------------------


def test_no_access_response_schema_carries_password_fields() -> None:
    """SPEC-10 T037/SC-003: ProxyProviderResponse (and its sibling response
    schemas) never expose `password`/`password_encrypted`/
    `password_key_version` — only `has_password` on the proxy-provider
    response."""
    import app.schemas.access as access_schemas

    response_classes = [
        access_schemas.ProxyProviderResponse,
        access_schemas.AccessPolicyResponse,
        access_schemas.DomainAccessRuleResponse,
    ]

    for cls in response_classes:
        field_names = set(cls.model_fields.keys())
        leaked = field_names & FORBIDDEN_PASSWORD_FIELDS
        assert not leaked, f"{cls.__name__} exposes forbidden field(s): {sorted(leaked)}"

    # ProxyProviderResponse must still surface the redaction signal.
    assert "has_password" in access_schemas.ProxyProviderResponse.model_fields


def test_no_access_schema_field_in_module_carries_password_ciphertext() -> None:
    """Belt-and-suspenders sweep: no *Response class anywhere in
    apps/api/app/schemas/access.py (present or future) may declare a
    `password_encrypted`/`password_key_version` field — those columns must
    never leave the ORM layer, on any response envelope in this module."""
    import app.schemas.access as access_schemas

    for name in dir(access_schemas):
        if not name.endswith("Response"):
            continue
        cls = getattr(access_schemas, name)
        model_fields = getattr(cls, "model_fields", None)
        if model_fields is None:
            continue
        leaked = set(model_fields.keys()) & {"password_encrypted", "password_key_version"}
        assert not leaked, f"{name} exposes forbidden field(s): {sorted(leaked)}"


# --- 2. spider never logs a decrypted password --------------------------------

_SPIDER_PATH = (
    REPO_ROOT
    / "apps"
    / "scrapers"
    / "price_monitor"
    / "spiders"
    / "generic_price_spider.py"
)

# Identifiers that, if referenced (as a Name or the attribute name in an
# Attribute access) inside a logging call's arguments, would mean a
# decrypted secret (or the token built from it) is being logged.
_FORBIDDEN_LOG_IDENTIFIERS = {
    "password",
    "provider_passwords",
    "_provider_passwords",
    "token",
}


def _is_logging_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}:
        return False
    value = func.value
    # logger.warning(...) / self.logger.error(...) / self.log(...)
    if isinstance(value, ast.Name) and value.id in {"logger", "log"}:
        return True
    if isinstance(value, ast.Attribute) and value.attr in {"logger", "log"}:
        return True
    return False


def _referenced_identifiers(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def test_spider_source_defines_logging_calls_to_inspect() -> None:
    """Sanity check that the guard below isn't vacuously passing because it
    failed to find any logging call sites at all."""
    tree = ast.parse(_SPIDER_PATH.read_text(encoding="utf-8"), filename=str(_SPIDER_PATH))
    call_sites = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_logging_call(node)]
    assert call_sites, "expected at least one logger.* call site in generic_price_spider.py"


def test_spider_never_logs_a_decrypted_password() -> None:
    """SPEC-10 T037: no `logger.<level>(...)` call site in the spider may
    reference the decrypted-password variables/attributes
    (`password`/`provider_passwords`/`_provider_passwords`/`token`) among
    its arguments -- only opaque identifiers (e.g. `proxy_provider_id`) may
    appear in a log message or its formatting args."""
    tree = ast.parse(_SPIDER_PATH.read_text(encoding="utf-8"), filename=str(_SPIDER_PATH))

    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_logging_call(node)):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            leaked = _referenced_identifiers(arg) & _FORBIDDEN_LOG_IDENTIFIERS
            if leaked:
                violations.append(f"line {node.lineno}: references {sorted(leaked)}")

    assert not violations, "logging call(s) may leak a decrypted password:\n" + "\n".join(violations)


# --- 3. budget.py never scans request_attempts --------------------------------

_BUDGET_PATH = REPO_ROOT / "libs" / "shared" / "app_shared" / "access" / "budget.py"


def test_budget_module_never_references_request_attempts() -> None:
    """SPEC-10 T037/FR-010/§22: `app_shared/access/budget.py` must never
    reference `request_attempts`/`RequestAttempt` anywhere in its source --
    the monthly-budget/ceiling/cooldown counters are pure Redis
    INCR/EXPIRE/SET-NX-EX math, never a scan of the attempts table."""
    contents = _BUDGET_PATH.read_text(encoding="utf-8")
    assert "request_attempts" not in contents
    assert "RequestAttempt" not in contents
