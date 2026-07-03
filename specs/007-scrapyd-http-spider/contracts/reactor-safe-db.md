# Contract: reactor-safe DB seam (`scrape_core.db`)

**Decided once** for the whole scraping runtime (FR-017, Principle V, research D1). Synchronous SQLAlchemy wrapped in Twisted `deferToThread`, reusing the SPEC-02 session/RLS seam through PgBouncer. No async DB stack.

## Surface

- `run_in_thread(fn, /, *args, **kwargs) -> twisted.internet.defer.Deferred`
  - Offloads `fn` to a reactor thread pool via `twisted.internet.threads.deferToThread`. The **only** sanctioned way a pipeline/middleware performs a DB (or other blocking) call. Never call a synchronous DB commit directly on the reactor thread.
- `workspace_txn(workspace_id) -> ContextManager[Session]`
  - Opens `app_shared.database.get_session()`, calls `set_workspace_context(session, workspace_id)` (activating RLS for the transaction), yields the session, commits on clean exit / rolls back on exception, closes. Runs **inside** the thread offloaded by `run_in_thread`, never on the reactor.

## Guarantees

- No blocking DB call executes on the reactor thread (Principle V; US5 scenario 2).
- Connections go through PgBouncer transaction pooling with a **small** per-process pool (`DB_POOL_SIZE`, existing; `prepare_threshold=None` already set in `app_shared.database`); only `SET LOCAL`/`set_config(...,true)` state (no session advisory locks / prepared statements).
- Workspace context is set on every transaction so DB RLS is the fail-closed second isolation layer.

## Tests (unit, no reactor required)

- `run_in_thread` returns a `Deferred` and offloads work off the calling thread (assert the callable runs via the thread-pool seam, mocked).
- `workspace_txn` issues `set_config('app.workspace_id', :wsid, true)` before yielding and commits/rolls back correctly.
