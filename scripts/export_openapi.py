"""Write the cleaned public OpenAPI spec to a file.

Usage:
    uv run python scripts/export_openapi.py docs/openapi-public.json

The SaaS `/docs` page (PLAN §7.4, Phase 4) is generated from this
document, so it is committed rather than produced at request time — a
customer-facing reference should not change because a route was renamed
mid-deploy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app
from app.openapi_public import build_public_openapi


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/openapi-public.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_public_openapi(app), indent=2) + "\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
