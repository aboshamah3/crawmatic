"""Reactor-safety proof (SPEC-11 Polish, T033, `contracts/reactor-seam.md`
DECISION OF RECORD; FR-007, SC-005) + the FR-016 negative check that
`price_analysis` never depends on the scrape lock.

Scenario 7 of `quickstart.md`: "no `time.sleep` and no sync `redis`/`EVAL`
outside `run_in_thread` in the spider/pipeline scrape path."

This is a **static** (no infra, no reactor, no Redis) AST-based grep test —
not a runtime check — so it runs in every environment, including this one
with no Docker daemon.

Approach for the five reactor-adjacent modules
(`apps/scrapers/price_monitor/spiders/generic_price_spider.py`,
`apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py`
(SPEC-14 T037b — the browser spider's `start`/`parse`/`errback` never call
a sync Session commit or blocking Redis directly; every DB/Redis-cache
touch it needs goes through `scrape_core.targets.load_targets`/
`_prepare_dispatch` via `run_in_thread`, never inline on the reactor
thread),
`libs/scrape-core/scrape_core/{limiter,pipelines,reactor}.py`):

1. Find every call to `run_in_thread(...)`/`deferToThread(...)` in the
   module and record the bare-name function reference handed to it as a
   "thread-boundary entry point" (e.g. `run_in_thread(load_targets, ...)`
   -> `load_targets`). Only bare-`Name` references count — a dotted
   reference like `_bucket.acquire_token` names a function defined in a
   *different* module (out of scope here; that module's own reactor
   safety is a separate concern) and is deliberately not matched against
   this module's own function defs, so it can never accidentally collide
   with a same-named local function.
2. Expand that entry set transitively: any locally-defined function
   called (again, by bare name) from inside an already-safe function is
   itself inside the sanctioned off-reactor boundary (e.g. `load_targets`
   calls `_cache_get_group_result`, so both are "safe").
3. Everything **not** in that safe set is assumed to run on (or be
   reachable synchronously from) the Twisted reactor thread. Assert none
   of those functions directly call `time.sleep(...)`, a synchronous
   Redis method (`redis.get(...)`, `get_redis_client().set(...)`, etc.),
   or a synchronous SQLAlchemy `Session.commit()`/`.rollback()`.

This mirrors the actual call graph as written today: `load_targets`/
`_prepare_dispatch`/`_mark_target_deferred_rate_limited` (HTTP spider),
`load_targets`/`_prepare_dispatch` again (browser spider — imported from
`scrape_core.targets`, so the browser spider's own `run_in_thread(...)`
call sites name the same shared functions), and `_flush_batch` (pipeline)
are the only functions ever handed to `run_in_thread`, and every
synchronous Redis/Session touch in these five modules lives inside one of
those (or something they call), never inside an `async def` coroutine or
a Scrapy pipeline hook that runs directly on the reactor thread.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SPIDER_PATH = _REPO_ROOT / "apps/scrapers/price_monitor/spiders/generic_price_spider.py"
_BROWSER_SPIDER_PATH = (
    _REPO_ROOT / "apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py"
)
_LIMITER_PATH = _REPO_ROOT / "libs/scrape-core/scrape_core/limiter.py"
_PIPELINES_PATH = _REPO_ROOT / "libs/scrape-core/scrape_core/pipelines.py"
_REACTOR_PATH = _REPO_ROOT / "libs/scrape-core/scrape_core/reactor.py"

_TARGET_PATHS = (
    _SPIDER_PATH,
    _BROWSER_SPIDER_PATH,
    _LIMITER_PATH,
    _PIPELINES_PATH,
    _REACTOR_PATH,
)

_THREAD_BOUNDARY_CALLERS = {"run_in_thread", "deferToThread"}

#: Synchronous redis-client method names that must never be invoked
#: directly outside the thread-pool boundary (Lua `EVAL`/`EVALSHA` are
#: registered/invoked via `register_script`, so that name is included too).
_REDIS_METHOD_NAMES = {
    "get",
    "set",
    "mget",
    "mset",
    "delete",
    "exists",
    "eval",
    "evalsha",
    "register_script",
    "script_load",
    "zadd",
    "zrem",
    "zremrangebyscore",
    "zcard",
    "zrangebyscore",
    "expire",
    "pexpire",
    "ttl",
    "pttl",
    "incr",
    "decr",
    "setnx",
    "hget",
    "hset",
}
#: Object names/hints that indicate the attribute call above is really a
#: Redis client method (as opposed to, say, an unrelated `dict.get(...)`
#: or `meta.get(...)` call, which would otherwise false-positive constantly).
_REDIS_OBJECT_NAME_HINTS = {"redis", "_redis", "client"}


def _iter_calls(node: ast.AST) -> "list[ast.Call]":
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)]


def _call_func_attr_or_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _bare_name_first_arg(call: ast.Call) -> str | None:
    """The bare-`Name` first positional argument of `call`, if any — used to
    find what function was actually handed to `run_in_thread`/`deferToThread`."""
    if not call.args:
        return None
    first = call.args[0]
    return first.id if isinstance(first, ast.Name) else None


def _looks_like_redis_object(value: ast.AST) -> bool:
    if isinstance(value, ast.Name):
        return value.id in _REDIS_OBJECT_NAME_HINTS
    if isinstance(value, ast.Call):
        callee = _call_func_attr_or_name(value)
        return callee is not None and "redis" in callee.lower()
    return False


def _is_sync_redis_call(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _REDIS_METHOD_NAMES
        and _looks_like_redis_object(func.value)
    )


def _is_time_sleep_call(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "sleep"
        and isinstance(func.value, ast.Name)
        and func.value.id == "time"
    )


#: SPEC-14 T037b: object names/hints that indicate a `.commit()`/
#: `.rollback()` attribute call below is really a SQLAlchemy `Session`
#: (as opposed to some unrelated object that happens to expose a
#: same-named method) — mirrors `_REDIS_OBJECT_NAME_HINTS`'s convention.
_SESSION_METHOD_NAMES = {"commit", "rollback"}
_SESSION_OBJECT_NAME_HINTS = {"session", "sess", "db_session"}


def _looks_like_session_object(value: ast.AST) -> bool:
    if isinstance(value, ast.Name):
        return value.id.lower() in _SESSION_OBJECT_NAME_HINTS
    if isinstance(value, ast.Call):
        callee = _call_func_attr_or_name(value)
        return callee is not None and "session" in callee.lower()
    return False


def _is_sync_session_commit_call(call: ast.Call) -> bool:
    """A direct (unawaited) `session.commit()`/`session.rollback()` call —
    the reactor-safety contract is that every such call happens inside
    `scrape_core.db.workspace_txn`, itself only ever entered from a
    callable already running off-reactor via `run_in_thread`/
    `deferToThread` (never directly in a Scrapy spider's `parse`/`start`/
    `errback` or a downloader middleware, which run on the reactor
    thread)."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _SESSION_METHOD_NAMES
        and _looks_like_session_object(func.value)
    )


def _local_function_defs(tree: ast.Module) -> "dict[str, ast.AST]":
    """Every module-level *and* class-level function/method def in this one
    module, keyed by its own (unqualified) name — a single-module call-graph
    walk, so cross-module collisions are not a concern here."""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _thread_boundary_entry_points(tree: ast.Module) -> "set[str]":
    entries: set[str] = set()
    for call in _iter_calls(tree):
        if _call_func_attr_or_name(call) in _THREAD_BOUNDARY_CALLERS:
            name = _bare_name_first_arg(call)
            if name is not None:
                entries.add(name)
    return entries


def _expand_transitively(entries: "set[str]", defs: "dict[str, ast.AST]") -> "set[str]":
    safe: set[str] = set()
    frontier = [name for name in entries if name in defs]
    while frontier:
        name = frontier.pop()
        if name in safe:
            continue
        safe.add(name)
        for call in _iter_calls(defs[name]):
            func = call.func
            if isinstance(func, ast.Name) and func.id in defs and func.id not in safe:
                frontier.append(func.id)
    return safe


def _violations_in_file(path: pathlib.Path) -> "list[str]":
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defs = _local_function_defs(tree)
    safe_names = _expand_transitively(_thread_boundary_entry_points(tree), defs)

    violations: list[str] = []
    for name, node in defs.items():
        if name in safe_names:
            continue
        for call in _iter_calls(node):
            if _is_time_sleep_call(call):
                violations.append(f"{path}:{call.lineno}: time.sleep() call in {name}()")
            elif _is_sync_redis_call(call):
                assert isinstance(call.func, ast.Attribute)
                violations.append(
                    f"{path}:{call.lineno}: synchronous redis `.{call.func.attr}()` call in "
                    f"{name}() outside a run_in_thread/deferToThread boundary"
                )
            elif _is_sync_session_commit_call(call):
                assert isinstance(call.func, ast.Attribute)
                violations.append(
                    f"{path}:{call.lineno}: synchronous session `.{call.func.attr}()` call in "
                    f"{name}() outside a run_in_thread/deferToThread boundary"
                )
    return violations


@pytest.mark.parametrize("path", _TARGET_PATHS, ids=lambda p: p.name)
def test_no_time_sleep_or_sync_redis_outside_thread_boundary(path: pathlib.Path) -> None:
    violations = _violations_in_file(path)
    assert not violations, "\n".join(violations)


# --- FR-016 negative check (analyze C1) -------------------------------------

_SCRAPE_LOCK_SYMBOLS = ("lock:scrape", "acquire_match_lock", "release_match_lock")


def test_tasks_analysis_never_references_the_scrape_lock() -> None:
    """`price_analysis` (`recompute_variant`) runs strictly after the scrape
    lock has already been released by the persistence pipeline (T023) — it
    must never itself acquire/release/reference the scrape-lock key family,
    or a future change could silently reintroduce a same-match dependency
    between the two queues."""
    path = _REPO_ROOT / "apps/workers/app/workers/tasks_analysis.py"
    contents = path.read_text(encoding="utf-8")
    leaked = [symbol for symbol in _SCRAPE_LOCK_SYMBOLS if symbol in contents]
    assert not leaked, f"{path} references scrape-lock symbol(s): {leaked}"


# --- SPEC-12 US5 (T038, contracts/stats-buffer.md "Called only from") ------
#
# The buffered attempt-stats recorder (`app_shared.strategy.stats_buffer
# .record_attempt`) is deliberately narrow: it must be reachable from
# exactly one call site, `_flush_batch` in `pipelines.py` (already inside
# `run_in_thread`, off-reactor). This is a *stronger* claim than "no sync
# Redis outside the thread boundary" above (which `record_attempt` itself
# would already satisfy once called from `_flush_batch`) -- it also
# forecloses a future refactor that calls it from some OTHER
# locally-defined function in `pipelines.py` (which might not itself be
# reachable from `run_in_thread`) or reintroduces it into the
# reactor-adjacent spider/limiter/reactor modules directly.


def _functions_calling(tree: ast.Module, called_name: str) -> "set[str]":
    """Names of every locally-defined function/method in `tree` containing
    at least one call whose attribute or bare name is `called_name`."""
    defs = _local_function_defs(tree)
    callers: set[str] = set()
    for name, node in defs.items():
        for call in _iter_calls(node):
            if _call_func_attr_or_name(call) == called_name:
                callers.add(name)
                break
    return callers


def test_strategy_stats_recorder_is_only_reachable_from_flush_batch() -> None:
    """`record_attempt` is called from exactly `_flush_batch` in
    `pipelines.py` (T037) -- never from any other locally-defined function
    in this module, and never referenced at all in the spider/limiter/
    reactor modules (it is Redis-only telemetry, never something the
    reactor-adjacent seams need to know about directly)."""
    pipelines_tree = ast.parse(
        _PIPELINES_PATH.read_text(encoding="utf-8"), filename=str(_PIPELINES_PATH)
    )
    callers = _functions_calling(pipelines_tree, "record_attempt")
    assert callers == {"_flush_batch"}, callers

    for path in (_SPIDER_PATH, _LIMITER_PATH, _REACTOR_PATH):
        contents = path.read_text(encoding="utf-8")
        assert "record_attempt" not in contents, f"{path} references record_attempt"
