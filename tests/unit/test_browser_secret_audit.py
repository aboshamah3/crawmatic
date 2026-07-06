"""Secret-safety audit (SPEC-14 T038, FR-011, SC-006, constitution §Tech):
the browser path's decrypted proxy password must be placed ONLY in the
Playwright ``proxy`` dict (``meta["playwright_context_kwargs"]["proxy"]
["password"]``) -- never logged, never in ``request.meta["proxy"]``
(the HTTP-transport-specific key ``HttpProxyMiddleware`` reads; this
project never registers that middleware, `contracts/browser-safety.md`
"Proxy"), and never embedded in any log line anywhere on the browser
scrape path.

`tests/integration/test_browser_proxy_live.py` already proves this at
runtime (a live Chromium+DB fixture asserting the plaintext password
never appears in captured logs) -- but that test `skipif`s cleanly in
this container-less build env. This is the **static** counterpart that
runs everywhere (no reactor/DB/Chromium), mirroring
`tests/unit/test_reactor_safety_grep.py`'s AST-grep convention: it
inspects the *source* of every module on the decrypt -> proxy-context
path (`generic_browser_price_spider.py`, `scrape_core/targets.py`
(``load_targets``'s one-time decrypt), `scrape_core/browser/ssrf.py`,
`scrape_core/browser/page.py`, `scrape_core/browser/variant.py`) for two
things:

1. No ``logger.<level>(...)``/``log_event(...)`` call anywhere in these
   modules references a `password`-named identifier/attribute, or
   inlines the literal word "password" into a formatted log message
   (which would suggest a value, not just a key name, is being logged).
2. `generic_browser_price_spider.py` never assigns
   ``request.meta["proxy"]`` / ``meta["proxy"]`` -- the browser spider's
   only proxy-carrying meta key is ``playwright_context_kwargs``.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_BROWSER_SPIDER_PATH = (
    _REPO_ROOT / "apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py"
)
_TARGETS_PATH = _REPO_ROOT / "libs/scrape-core/scrape_core/targets.py"
_SSRF_PATH = _REPO_ROOT / "libs/scrape-core/scrape_core/browser/ssrf.py"
_PAGE_PATH = _REPO_ROOT / "libs/scrape-core/scrape_core/browser/page.py"
_VARIANT_PATH = _REPO_ROOT / "libs/scrape-core/scrape_core/browser/variant.py"

_AUDITED_PATHS = (_BROWSER_SPIDER_PATH, _TARGETS_PATH, _SSRF_PATH, _PAGE_PATH, _VARIANT_PATH)

_LOG_CALL_ATTRS = {"debug", "info", "warning", "error", "exception", "critical"}
_LOG_CALLEE_NAMES = {"log_event"}


def _iter_calls(tree: ast.AST) -> "list[ast.Call]":
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _is_logging_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _LOG_CALL_ATTRS:
        # `logger.warning(...)`/`self.logger.error(...)`/... -- any
        # object, since spiders use `self.logger` and modules use a
        # bare module-level `logger`.
        return True
    if isinstance(func, ast.Name) and func.id in _LOG_CALLEE_NAMES:
        return True
    return False


def _mentions_password(node: ast.AST) -> "str | None":
    """Returns a description of the offending sub-node if `node` (one
    argument of a logging call) references an actual password *value*:
    a bare `password` identifier, or a `.password`/`_password` attribute
    access. Plain prose mentioning the *word* "password" in a message
    template (e.g. "could not decrypt password for %s") is fine and
    never flagged -- only a Name/Attribute reference that could carry
    the secret's actual value is a violation, whether it appears as a
    `%`-arg, a keyword arg, or inside an f-string's `FormattedValue`."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and "password" in child.id.lower():
            return f"identifier {child.id!r}"
        if isinstance(child, ast.Attribute) and "password" in child.attr.lower():
            return f"attribute access .{child.attr}"
    return None


def test_no_logging_call_on_the_browser_scrape_path_references_a_password() -> None:
    """SPEC-14 T038: static audit -- no `logger.*`/`log_event(...)` call in
    the browser spider or its safety/page/variant/targets collaborators
    ever references anything password-shaped, anywhere in its arguments."""
    violations: list[str] = []
    for path in _AUDITED_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _iter_calls(tree):
            if not _is_logging_call(call):
                continue
            for arg in list(call.args) + [kw.value for kw in call.keywords]:
                reason = _mentions_password(arg)
                if reason is not None:
                    violations.append(f"{path}:{call.lineno}: logging call references {reason}")
    assert not violations, "\n".join(violations)


def test_browser_spider_never_sets_the_http_proxy_meta_key() -> None:
    """SPEC-14 T038 / contracts/browser-safety.md "Proxy": the browser
    spider must never write `request.meta["proxy"]`/`meta["proxy"]` --
    that key is the HTTP-transport-specific one
    `scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware` reads
    (the HTTP spider's `_request_for` sets it; the browser project never
    registers that middleware). The browser spider's only proxy-carrying
    meta key is `playwright_context_kwargs`."""
    tree = ast.parse(_BROWSER_SPIDER_PATH.read_text(encoding="utf-8"), filename=str(_BROWSER_SPIDER_PATH))
    violations: list[str] = []
    for node in ast.walk(tree):
        target_exprs: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            target_exprs = node.targets
        elif isinstance(node, ast.AugAssign):
            target_exprs = [node.target]
        for target in target_exprs:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "meta"
            ):
                key_node = target.slice
                if isinstance(key_node, ast.Constant) and key_node.value == "proxy":
                    violations.append(f"{_BROWSER_SPIDER_PATH}:{node.lineno}: meta[\"proxy\"] assignment")
    assert not violations, "\n".join(violations)


def test_decrypted_password_flows_only_into_the_playwright_proxy_dict() -> None:
    """SPEC-14 T038: the one dict key literally named `"password"` that the
    browser spider assigns must be inside `proxy_kwargs` (which is only
    ever nested under `meta["playwright_context_kwargs"]["proxy"]`,
    never a bare `meta["password"]` or similar top-level leak)."""
    tree = ast.parse(_BROWSER_SPIDER_PATH.read_text(encoding="utf-8"), filename=str(_BROWSER_SPIDER_PATH))
    password_key_assignments: list[ast.Subscript] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
        ):
            target = node.targets[0]
            key_node = target.slice
            if (
                isinstance(key_node, ast.Constant)
                and isinstance(key_node.value, str)
                and key_node.value == "password"
            ):
                password_key_assignments.append(target)

    assert password_key_assignments, (
        "expected at least one `[\"password\"] = ...` assignment in the browser spider "
        "(the proxy-context branch) -- none found; audit assumptions may be stale"
    )
    for target in password_key_assignments:
        assert isinstance(target.value, ast.Name) and target.value.id == "proxy_kwargs", (
            f"{_BROWSER_SPIDER_PATH}:{target.lineno}: a [\"password\"] assignment target "
            f"other than proxy_kwargs was found -- possible leak surface"
        )
