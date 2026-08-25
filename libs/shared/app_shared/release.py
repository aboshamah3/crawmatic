"""Release identity — what this running process actually IS (READY-001, Task A5).

Audit ref: `PRODUCTION_READINESS_IMPLEMENTATION_PLAN_2026-08-25.md` Task A5
("Release identity — build manifest, deployment attestation, `/version`,
external probes"), which in turn closes
`CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md` §C2 ("Release source
of truth is not controlled").

Two linked records, never one mutable file
------------------------------------------

* The **build manifest** (`scripts/build_release_manifest.py`) is produced at
  BUILD time and is immutable: it self-hashes into a `manifest_id`, and an
  optional GPG detached signature is layered on top. It records image
  digests, the source digest, the API schema version, the migration set, the
  config *schema* (names and types — never values), external component
  versions, and links to test/scan evidence.
* The **deployment attestation** (`scripts/build_deployment_attestation.py`)
  is produced at DEPLOY time and references the build manifest *by digest*.
  It records the environment, the pre-migration backup id, the approver, the
  deployment timestamp, the live database migration head, smoke results and
  rollback artifacts.

The split exists so deploy-time facts never require mutating — and therefore
re-signing, and therefore casting doubt on — the build-time record.

Why baked beats environment
---------------------------

A Railway environment variable is editable by anyone with dashboard access,
at any time, with no audit trail tied to the image that is actually running.
If release truth came from env, "which build is live?" would be answerable
only by "whatever someone last typed", which is exactly the failure mode §C2
names. So identity is **baked into the image at build time** — a generated
`release_identity.json` written next to the package (or an
`app_shared._baked_release` module, or a path named by
``CRAWMATIC_RELEASE_IDENTITY_PATH``).

Environment variables are still read, but only as a **mirror**: when a
mirrored value disagrees with the baked value, the baked value is what
`/version` reports and the disagreement is published in
``env_mismatches``. Neither value is silently preferred and neither is
silently dropped — an operator seeing a non-empty ``env_mismatches`` knows
the deployment's env has drifted from the artifact.

An image with nothing baked is not an error. It degrades to
``identity_source="env"`` (if any mirror is set) or ``"unavailable"``, and
the migration provenance `/version` has published since §C2 keeps working
regardless — an unbaked build is less legible, not broken.

Secret discipline
-----------------

`/version` and `/ready` are unauthenticated (see those routers' docstrings).
This module therefore publishes a **digest** of the config schema and a list
of setting NAMES and TYPES; it never reads, hashes, or exposes a setting's
VALUE. `config_schema()` reads `Settings.model_fields` — the pydantic field
*declarations* on the class — and never constructs a `Settings` instance, so
no environment value is ever in scope here.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ENV_MIRRORS",
    "IDENTITY_FIELDS",
    "RELEASE_IDENTITY_FILENAME",
    "RELEASE_IDENTITY_PATH_ENV",
    "ReleaseIdentity",
    "canonical_json",
    "code_migration_head",
    "config_schema",
    "compute_config_schema_version",
    "get_release_identity",
    "reset_release_identity_cache",
    "sha256_digest",
]

#: Points at a JSON identity file written into the image at build time
#: (`build_release_manifest.py --emit-identity`). Highest-precedence baked
#: source: a Dockerfile can `COPY` the file anywhere and name it here.
RELEASE_IDENTITY_PATH_ENV = "CRAWMATIC_RELEASE_IDENTITY_PATH"

#: Conventional filename looked for next to the `app_shared` package and at
#: the repo/app root when the env var above is unset.
RELEASE_IDENTITY_FILENAME = "release_identity.json"

#: The identity fields a build bakes. `live_db_migration` is deliberately NOT
#: here: it is a request-time reading of the database, not a build fact.
IDENTITY_FIELDS = (
    "manifest_id",
    "source_digest",
    "image_digest",
    "config_schema_version",
    "expected_db_migration",
)

#: Environment variables that may MIRROR the baked identity. Baked wins; a
#: disagreement is surfaced, never resolved silently.
ENV_MIRRORS = {
    "manifest_id": "RELEASE_MANIFEST_ID",
    "source_digest": "RELEASE_SOURCE_DIGEST",
    "image_digest": "RELEASE_IMAGE_DIGEST",
    "config_schema_version": "RELEASE_CONFIG_SCHEMA_VERSION",
    "expected_db_migration": "RELEASE_EXPECTED_DB_MIGRATION",
}


@dataclass(frozen=True)
class ReleaseIdentity:
    """What this process is, and where each part of that answer came from.

    Every identity field is `str | None`: an unbaked image reports `None`
    rather than a guess or a placeholder, because a plausible-looking wrong
    digest is worse during an incident than an honest absence.
    """

    manifest_id: str | None
    source_digest: str | None
    image_digest: str | None
    config_schema_version: str | None
    expected_db_migration: str | None
    live_db_migration: str | None
    #: ``"baked"`` | ``"env"`` | ``"unavailable"`` — provenance of the four
    #: build-time fields above, so a reader never has to guess whether they
    #: are looking at an artifact fact or a dashboard-typed one.
    identity_source: str = "unavailable"
    #: Names of identity fields whose ``ENV_MIRRORS`` env var disagrees with
    #: the baked value. Non-empty means the deployment's environment has
    #: drifted from the artifact it is running.
    env_mismatches: tuple[str, ...] = ()

    @property
    def migration_heads_match(self) -> bool | None:
        """``None`` when either head is unknown — never a guess."""
        if self.expected_db_migration is None or self.live_db_migration is None:
            return None
        return self.expected_db_migration == self.live_db_migration


# --------------------------------------------------------------------------
# Deterministic hashing helpers (shared with the two builder scripts)
# --------------------------------------------------------------------------


def canonical_json(payload: Any) -> str:
    """Byte-stable JSON: sorted keys, no insignificant whitespace, UTF-8 text.

    Both builder scripts and this module hash through here so a manifest
    built twice from the same inputs produces the same `manifest_id`.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_digest(data: str | bytes) -> str:
    """``"sha256:<hex>"`` — prefixed so a digest is never mistaken for a
    revision id, a git SHA, or an opaque token in a log line."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------
# Config schema: NAMES and TYPES only, never values
# --------------------------------------------------------------------------


def config_schema() -> list[dict[str, Any]]:
    """The declared settings surface: ``[{name, type, required}, ...]``, sorted.

    Reads `Settings.model_fields` (the class-level pydantic declarations) —
    it never constructs a `Settings`, so no environment value is ever read,
    let alone published. Each entry carries exactly three keys and there is
    deliberately no `default`: a default IS a value, and a default that
    happens to be a real credential in some future field would leak through
    an unauthenticated endpoint.
    """
    from app_shared.config import Settings

    fields: list[dict[str, Any]] = []
    for name, field in Settings.model_fields.items():
        annotation = field.annotation
        type_name = getattr(annotation, "__name__", None) or str(annotation)
        fields.append(
            {"name": name, "type": type_name, "required": bool(field.is_required())}
        )
    return sorted(fields, key=lambda item: item["name"])


def compute_config_schema_version() -> str:
    """A deterministic digest over the config schema.

    Changes exactly when a setting is added, removed, renamed, retyped, or
    flips required/optional — which is the thing a deploy needs to notice
    ("this image expects a variable this environment has never had").
    """
    return sha256_digest(canonical_json(config_schema()))


# --------------------------------------------------------------------------
# Code-expected migration head
# --------------------------------------------------------------------------

#: `libs/shared/app_shared/release.py` -> app_shared -> shared -> libs -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_ini_path() -> Path:
    """`alembic.ini` at the repo root, which is also the image's `/app` root
    (`.dockerignore` excludes `tests`/`specs`/`.git` but keeps `alembic/` and
    `alembic.ini`, and `apps/migrate/Dockerfile` documents that it needs
    them at runtime)."""
    return _REPO_ROOT / "alembic.ini"


#: Sentinel distinguishing "not yet resolved" from a resolved ``None``, so a
#: genuinely unresolvable head is cached as the negative result it is rather
#: than being retried on every request.
_UNRESOLVED = object()

_cached_code_head: Any = _UNRESOLVED


def code_migration_head() -> str | None:
    """The single head `alembic/versions/*.py` resolves to, or ``None``.

    Never raises. `alembic` is a dev-group dependency, not an `app_shared`
    runtime dependency, so the import is lazy and guarded: a service image
    without it reports ``None`` instead of failing to import this module.
    Mirrors `apps/api/app/routers/version._code_migration_head`, which is
    also what `scripts/check_single_head.sh` resolves (DB-independent).

    Memoised per process. `/ready` calls this on every probe (an orchestrator
    hits it every few seconds) and resolving a head walks the whole
    `alembic/versions/` directory and parses each revision — real filesystem
    work to re-derive a value that is baked into the image's filesystem and
    therefore cannot change while the process lives. `reset_release_identity_cache()`
    clears it.
    """
    global _cached_code_head
    if _cached_code_head is not _UNRESOLVED:
        return _cached_code_head  # type: ignore[return-value]

    resolved: str | None = None
    ini = _alembic_ini_path()
    if ini.exists():
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            resolved = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
        except Exception:  # noqa: BLE001 - missing/forked revision graph reported as None
            resolved = None
    _cached_code_head = resolved
    return resolved


def migration_revisions() -> list[str]:
    """Every revision id in the running code's script directory, oldest first.

    The build manifest records the whole *set*, not just the head: "which
    migrations does this artifact contain" is a different question from
    "which one is last", and only the set answers "was this revision ever in
    a shipped image?" after a branch is deleted. Empty list on any failure.
    """
    ini = _alembic_ini_path()
    if not ini.exists():
        return []
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config(str(ini)))
        return sorted(revision.revision for revision in script.walk_revisions())
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------
# Baked identity loading
# --------------------------------------------------------------------------


def _read_identity_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable/corrupt bake is "unbaked", never fatal
        return None
    return payload if isinstance(payload, dict) else None


def _baked_payload_from_env_path() -> dict[str, Any] | None:
    raw = os.environ.get(RELEASE_IDENTITY_PATH_ENV)
    if not raw:
        return None
    return _read_identity_file(Path(raw))


def _baked_payload_from_module() -> dict[str, Any] | None:
    """A generated `app_shared/_baked_release.py` with `RELEASE_IDENTITY: dict`.

    Supported alongside the JSON file because a generated *module* survives
    any working-directory or COPY-path surprise a JSON file can hit — it
    travels with the installed package itself.
    """
    try:
        from app_shared import _baked_release  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - absent in an unbaked build, which is normal
        return None
    payload = getattr(_baked_release, "RELEASE_IDENTITY", None)
    return payload if isinstance(payload, dict) else None


def _baked_payload_from_wellknown_files() -> dict[str, Any] | None:
    for candidate in (
        Path(__file__).resolve().parent / RELEASE_IDENTITY_FILENAME,
        _REPO_ROOT / RELEASE_IDENTITY_FILENAME,
    ):
        if candidate.exists():
            payload = _read_identity_file(candidate)
            if payload is not None:
                return payload
    return None


def _load_baked_payload() -> dict[str, Any] | None:
    """Baked sources in precedence order; the first that yields a dict wins."""
    for loader in (
        _baked_payload_from_env_path,
        _baked_payload_from_module,
        _baked_payload_from_wellknown_files,
    ):
        payload = loader()
        if payload is not None:
            return payload
    return None


# --------------------------------------------------------------------------
# The public entry point
# --------------------------------------------------------------------------

#: Per-process memo of the BUILD-time half of the identity only. It is a
#: build fact and cannot change while the process runs, so re-reading a file
#: on every `/version` request would be pure I/O for no new information.
#: `live_db_migration` is never memoised — it is a live database reading.
_cached_build_identity: tuple[dict[str, str | None], str, tuple[str, ...]] | None = None


def reset_release_identity_cache() -> None:
    """Drop every per-process memo (tests, and any deliberate re-read)."""
    global _cached_build_identity, _cached_code_head
    _cached_build_identity = None
    _cached_code_head = _UNRESOLVED


def _resolve_build_identity() -> tuple[dict[str, str | None], str, tuple[str, ...]]:
    global _cached_build_identity
    if _cached_build_identity is not None:
        return _cached_build_identity

    baked = _load_baked_payload()
    env_values = {
        field: os.environ.get(env_name) or None
        for field, env_name in ENV_MIRRORS.items()
    }

    values: dict[str, str | None] = {}
    mismatches: list[str] = []

    if baked is not None:
        source = "baked"
        for field in IDENTITY_FIELDS:
            raw = baked.get(field)
            baked_value = str(raw) if raw is not None else None
            values[field] = baked_value
            env_value = env_values.get(field)
            # Only a *present and differing* mirror is drift. An absent
            # mirror is the normal case and says nothing at all.
            if env_value is not None and baked_value is not None and env_value != baked_value:
                mismatches.append(field)
    elif any(env_values.values()):
        source = "env"
        values = dict(env_values)
    else:
        source = "unavailable"
        values = dict.fromkeys(IDENTITY_FIELDS)

    resolved = (values, source, tuple(sorted(mismatches)))
    _cached_build_identity = resolved
    return resolved


def get_release_identity(
    *,
    live_db_migration: str | None = None,
    expected_db_migration: str | None = None,
) -> ReleaseIdentity:
    """Assemble the full identity for this process.

    ``live_db_migration`` is passed in by the caller that owns a database
    session (`/version`, `/ready`) rather than opened here: this module is
    framework- and engine-agnostic and must never decide *how* to reach the
    database.

    ``expected_db_migration`` — the head the RUNNING CODE resolves — may be
    passed in by a caller that already computed it; otherwise
    `code_migration_head()` resolves it here. Note the deliberate ordering:
    the code-resolved head wins over the baked one. The baked manifest's
    migration head is a build-time *claim*; the script directory in this
    image is the running code's *fact*, and the whole purpose of `/version`
    is to compare that fact against the live database. A disagreement
    between the two is itself drift, and is reported through
    ``env_mismatches``-style surfacing on the `baked_expected_db_migration`
    key rather than being silently averaged away.
    """
    values, source, mismatches = _resolve_build_identity()

    resolved_expected = (
        expected_db_migration
        if expected_db_migration is not None
        else code_migration_head()
    )
    baked_expected = values.get("expected_db_migration")
    if resolved_expected is None:
        # Nothing better to report than the build's own claim.
        resolved_expected = baked_expected
    elif baked_expected is not None and baked_expected != resolved_expected:
        mismatches = tuple(sorted({*mismatches, "baked_expected_db_migration"}))

    return ReleaseIdentity(
        manifest_id=values.get("manifest_id"),
        source_digest=values.get("source_digest"),
        image_digest=values.get("image_digest"),
        config_schema_version=values.get("config_schema_version"),
        expected_db_migration=resolved_expected,
        live_db_migration=live_db_migration,
        identity_source=source,
        env_mismatches=mismatches,
    )
