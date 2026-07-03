"""`apps/api`-local orchestrators that drive `app_shared` pure cores with I/O.

Kept separate from `app_shared` because these modules drive Redis/DB I/O
directly (the SPEC-06 `profile_resolution` cache orchestrator is the
first tenant) — `app_shared` stays framework/I-O-boundary-agnostic
(Constitution I).
"""

from __future__ import annotations

__all__: list[str] = []
