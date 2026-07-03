"""API-layer Pydantic DTOs (SPEC-04).

Request/response models for the catalog endpoints live here (US1+) —
kept separate from ``app_shared`` so the framework-agnostic core never
depends on Pydantic/FastAPI. Empty package init for now.
"""

from __future__ import annotations

__all__: list[str] = []
