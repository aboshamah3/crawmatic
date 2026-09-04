"""Control-plane domain package (EPA C2, 2026-09-03).

The engine side of the SaaS control-plane contract: the service layer the
``/v1/admin/workspaces/{workspace_id}/control-plane`` routes call.
Framework-agnostic (SQLAlchemy only, no FastAPI) so the routes stay a
thin HTTP shell over it and the rules can be unit-tested without a
request.
"""

from __future__ import annotations
