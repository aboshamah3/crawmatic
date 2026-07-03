# Contract: assignment enforcement on existing endpoints (`routers/competitors.py`, `routers/matches.py`, extend)

FR-013: an assignment may reference only a profile **visible** to the assigning workspace (own or global); another workspace's profile is rejected; clearing (null) is allowed. The `scrape_profile_id`/`default_scrape_profile_id` fields already exist on the competitor and match schemas/routers (SPEC-05); this spec adds the visibility check.

## Where `assert_profile_assignable` is called

| Router / endpoint | Field | When |
|-------------------|-------|------|
| `competitors.py` `POST /v1/competitors` | `default_scrape_profile_id` | if set |
| `competitors.py` `PATCH /v1/competitors/{id}` | `default_scrape_profile_id` | if in the update |
| `matches.py` `POST /v1/matches` | `scrape_profile_id` | if set |
| `matches.py` `PATCH /v1/matches/{id}` | `scrape_profile_id` | if in the update |
| `matches.py` `POST /v1/matches/bulk-upsert` | `scrape_profile_id` per row | one `profile_visibility_map` IN(...) lookup for the batch's distinct ids, then per-row check |
| `scrape_profiles.py` `PUT /v1/scrape-profiles/workspace-default` | `profile_id` | if set |

## Behaviour (`app_shared.profiles.repository.assert_profile_assignable`)

- `None` → OK (clearing / not assigning).
- Own-workspace or global (`workspace_id IS NULL`) profile → OK.
- Dangling id → `404 NOT_FOUND`.
- Other-workspace profile → `422 WORKSPACE_MISMATCH`.

Reuses the `MissingReference`/`CrossWorkspaceReference` exceptions and the existing router error builders (`_not_found`, `_workspace_mismatch`) from SPEC-05 — no new error vocabulary on these routers.

## Bulk path (no N+1)

The match bulk-upsert collects the batch's distinct `scrape_profile_id`s and runs **one** `profile_visibility_map` (`visible_profiles_select` IN(...)) lookup, then checks each row against the in-memory map — never one query per row (Principle VIII), matching how competitor ids are already consistency-checked there.

## Tests

- **Unit (no DB)**: given an in-memory visibility map, each assignment path accepts own/global/null and rejects dangling/cross-ws.
- **Live (marked)**: assign own + global accepted; cross-workspace rejected on competitor, match (single + bulk), and workspace-default; clearing accepted; after `assert_profile_assignable` passes, the row persists; deleting the profile nulls the reference (`ON DELETE SET NULL`).
