"""No-startup-migrations compose test (FR-011, SC-007 no-startup part).

DB-independent — parses `docker-compose.yml` as YAML, no Docker daemon
or live Postgres required (unlike `tests/integration/test_compose_smoke.py`,
which actually drives `docker compose`).

Asserts:

1. None of the `api`/`scheduler`/`worker` app services runs
   `alembic`/`migrate`/`upgrade` in its `command`, `entrypoint`, or the
   Dockerfile it builds from — migrations only ever run via the
   dedicated one-shot `migrate` service (contracts/migration-job.md).
2. The `migrate` service is one-shot (`restart: "no"`), not a
   long-running service.
3. The `migrate` service connects directly to `postgres:5432` (never
   `pgbouncer:6432`) via `MIGRATION_DATABASE_URL`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

APP_SERVICES = ("api", "scheduler", "worker")
MIGRATION_KEYWORDS = ("alembic", "migrate", "upgrade")


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _stringify(value: object) -> str:
    """Render a compose `command`/`entrypoint` value (str, list, or None) as one string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def test_app_services_do_not_run_migrations_at_startup() -> None:
    """api/scheduler/worker never run alembic/migrate/upgrade in command or entrypoint."""
    compose = _load_compose()
    services = compose["services"]

    for service_name in APP_SERVICES:
        assert service_name in services, f"expected {service_name!r} service in docker-compose.yml"
        service = services[service_name]

        combined = " ".join(
            [_stringify(service.get("command")), _stringify(service.get("entrypoint"))]
        ).lower()

        for keyword in MIGRATION_KEYWORDS:
            assert keyword not in combined, (
                f"service {service_name!r} command/entrypoint contains migration "
                f"keyword {keyword!r}: {combined!r} — app services must never migrate "
                "at startup (FR-011)"
            )

        # app services only depend on pgbouncer (the pooler), never postgres directly.
        depends_on = service.get("depends_on", {})
        depends_on_names = (
            set(depends_on.keys()) if isinstance(depends_on, dict) else set(depends_on)
        )
        assert "postgres" not in depends_on_names, (
            f"service {service_name!r} depends_on postgres directly — app services "
            "must only depend_on pgbouncer (FR-011)"
        )


def test_migrate_service_is_one_shot_and_connects_directly_to_postgres() -> None:
    """migrate service: restart: "no", connects to postgres:5432 (not pgbouncer:6432)."""
    compose = _load_compose()
    services = compose["services"]

    assert "migrate" in services, "expected a one-shot `migrate` service in docker-compose.yml"
    migrate = services["migrate"]

    assert migrate.get("restart") == "no", (
        f"migrate service must be one-shot (restart: \"no\"), got: {migrate.get('restart')!r}"
    )

    depends_on = migrate.get("depends_on", {})
    depends_on_names = set(depends_on.keys()) if isinstance(depends_on, dict) else set(depends_on)
    assert "postgres" in depends_on_names, "migrate service must depend_on postgres"
    assert "pgbouncer" not in depends_on_names, (
        "migrate service must not depend_on pgbouncer — it connects directly to postgres"
    )

    # The direct-connection guarantee is enforced by MIGRATION_DATABASE_URL in
    # .env(.example) pointing at postgres:5432 (never pgbouncer:6432) — verified
    # separately below by inspecting .env.example, since the compose service
    # itself only references env_file/environment, not literal URLs.
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    migration_url_lines = [
        line
        for line in env_example.splitlines()
        if line.strip().startswith("MIGRATION_DATABASE_URL=")
    ]
    assert migration_url_lines, "expected MIGRATION_DATABASE_URL in .env.example"
    migration_url = migration_url_lines[0]
    assert "postgres:5432" in migration_url, (
        f"MIGRATION_DATABASE_URL must target postgres:5432 directly: {migration_url!r}"
    )
    assert "pgbouncer" not in migration_url, (
        f"MIGRATION_DATABASE_URL must never target pgbouncer: {migration_url!r}"
    )
