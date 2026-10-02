#!/usr/bin/env python3
"""Read-only dry run for the canonical-competitor-domain migration (E3/E4).

Lists (1) competitors that would collide or are invalid after
canonicalisation (the migration refuses on any) and (2) matches whose URL
host is foreign to their competitor's domain. Never writes: the session is
READ ONLY. The DSN comes ONLY from the environment (``DRYRUN_DATABASE_URL``,
else ``MIGRATION_DATABASE_URL``); it is never printed. Match URLs are
reported as host only (no path/query).
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

from app_shared.domains import plan_competitor_domain_rewrite, url_host_belongs_to_domain


def main() -> int:
    dsn = os.environ.get("DRYRUN_DATABASE_URL") or os.environ.get("MIGRATION_DATABASE_URL")
    if not dsn:
        print("Set DRYRUN_DATABASE_URL (or MIGRATION_DATABASE_URL) in the environment.", file=sys.stderr)
        return 2
    import psycopg

    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        comps = conn.execute("SELECT id, workspace_id, domain FROM competitors").fetchall()
        updates, collisions, invalid = plan_competitor_domain_rewrite(comps)
        print(f"competitors: {len(comps)}  would-rewrite: {len(updates)}")
        print(f"COLLISIONS ({len(collisions)}) - migration will REFUSE while any exist:")
        for ws, canon, ids in collisions:
            print(f"  workspace={ws} canonical={canon} competitor_ids={sorted(map(str, ids))}")
        print(f"INVALID ({len(invalid)}):")
        for cid, dom in invalid:
            print(f"  competitor_id={cid} domain={dom!r}")
        by_id = {c[0]: c[2] for c in comps}
        foreign = 0
        print("FOREIGN-HOST MATCHES:")
        cur = conn.execute("SELECT id, workspace_id, competitor_id, competitor_url FROM competitor_product_matches")
        for match_id, ws, comp_id, url in cur:
            dom = by_id.get(comp_id)
            if not url_host_belongs_to_domain(url, dom):
                foreign += 1
                host = urlsplit(url or "").hostname
                print(f"  match_id={match_id} workspace={ws} competitor_id={comp_id} competitor_domain={dom!r} url_host={host!r}")
        print(f"foreign-host matches: {foreign}")
    return 1 if collisions or invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
