"""Static lint of the DR backup scripts (EPA C10, F21).

The whole point of `apps/dr-backup/` is that the backup runs INSIDE Railway
and dumps over the private network: the 2026-09 deep dive measured 0.853
GB/day of idle egress caused by the ops host pulling full logical dumps across
the PUBLIC TCP proxy every four hours.

That property is invisible at runtime until a bill arrives, and it is exactly
one edit away from being lost -- somebody pasting a public host into a
variable default, or "temporarily" relaxing the guard in `backup.sh`. So it is
asserted statically here:

* no public hostname literal anywhere in the DR scripts or the service;
* `PGHOST` is built from a per-target variable and REFUSED unless it is a
  `*.railway.internal` private name;
* the host-side pull refuses plain HTTP to anything that is not a private
  name;
* the file server fails closed with no token.

These are text assertions on purpose. A behavioural test would need a Railway
project; this needs nothing, runs in the unit gate, and fails the moment the
guarantee is edited away.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DR_BACKUP = REPO_ROOT / "apps" / "dr-backup"
BACKUP_SH = DR_BACKUP / "backup.sh"
SERVE_PY = DR_BACKUP / "serve.py"
DOCKERFILE = DR_BACKUP / "Dockerfile"
RAILWAY_JSON = DR_BACKUP / "railway.json"
DR_LIB = REPO_ROOT / "scripts" / "dr" / "dr_lib.sh"
BACKUP_PROD = REPO_ROOT / "scripts" / "dr" / "backup_prod.sh"

SCANNED = [BACKUP_SH, SERVE_PY, DOCKERFILE, RAILWAY_JSON, DR_LIB, BACKUP_PROD]

# Public Railway surfaces, in every shape they appear in: the edge domain, the
# TCP proxy, and the legacy one. `*.railway.internal` is the private network
# and is the thing we WANT, so it is deliberately not matched here.
PUBLIC_HOST = re.compile(
    r"""[a-z0-9][a-z0-9-]*\.(?:up\.railway\.app|railway\.app|rlwy\.net|railway\.dev)""",
    re.IGNORECASE,
)
# A bare IPv4 literal, minus the loopback/any addresses the scratch-container
# plumbing legitimately uses.
IPV4 = re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b")


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_no_public_hostname_literal(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not PUBLIC_HOST.findall(text), (
        f"{path.relative_to(REPO_ROOT)} contains a PUBLIC Railway hostname literal "
        f"{PUBLIC_HOST.findall(text)!r}. The backup must reach production over "
        "*.railway.internal; a public host here silently reintroduces the "
        "0.853 GB/day of egress this service exists to remove."
    )


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_no_bare_ip_literal(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = [ip for ip in IPV4.findall(text)]
    assert not found, (
        f"{path.relative_to(REPO_ROOT)} contains an IP literal {found!r} — "
        "connections are made through private DNS names, never pinned addresses"
    )


def test_backup_pghost_comes_from_a_private_railway_name() -> None:
    text = BACKUP_SH.read_text(encoding="utf-8")
    # PGHOST is exported from the per-target variable, never from a literal.
    assert 'export PGHOST="$host"' in text, (
        "backup.sh must export PGHOST from the per-target *_PGHOST variable"
    )
    # …and only after asserting the value is a private name.
    guard = re.search(
        r'\[\[\s*"\$host"\s*==\s*\*\.railway\.internal\s*\]\]\s*\\\s*\n\s*\|\|\s*dr_die',
        text,
    )
    assert guard, (
        "backup.sh must dr_die when a target's *_PGHOST is not a "
        "*.railway.internal name — that guard IS the egress guarantee"
    )
    # The guard must come before the export, or it guards nothing.
    assert text.index("*.railway.internal") < text.index('export PGHOST="$host"')


def test_backup_never_puts_a_credential_on_argv() -> None:
    text = BACKUP_SH.read_text(encoding="utf-8")
    # dr_lib.sh design rule 1: pg_* are invoked with no DSN argument.
    assert not re.search(r"pg_dump[^\n]*postgres(?:ql)?://", text)
    assert not re.search(r"--dbname[= ]", text)
    assert "--passphrase-file" not in text or "--passphrase " not in text


def test_pull_side_refuses_plain_http_to_a_public_host() -> None:
    text = BACKUP_PROD.read_text(encoding="utf-8")
    assert "DR_PULL_BASE_URL" in text
    assert '[[ "$host" == *.railway.internal ]]' in text, (
        "the pull must refuse plain http to anything but a private name"
    )
    assert "dr_die" in text.split('[[ "$host" == *.railway.internal ]]')[1][:400], (
        "the private-name check must fail the run, not just warn"
    )


def test_pull_verifies_checksums_before_accepting_a_set() -> None:
    text = BACKUP_PROD.read_text(encoding="utf-8")
    assert "sha256sum -c --status SHA256SUMS" in text
    # …and refuses a set that arrives without them.
    assert "refusing to accept an unverifiable set" in text


def test_serve_fails_closed_and_binds_the_private_network() -> None:
    text = SERVE_PY.read_text(encoding="utf-8")
    assert "hmac.compare_digest" in text, "token comparison must be constant time"
    assert "bool(TOKEN) and" in text, "no token configured must refuse every request"
    assert "AF_INET6" in text and '(("::", PORT)' in text, (
        "must bind :: — Railway's private network is IPv6-only"
    )
    # Read-only: no write verbs are routed.
    for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
        assert verb not in text, f"{verb} must not exist on a backup file server"


def test_dockerfile_pins_the_production_postgres_major() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    from_line = next(ln for ln in text.splitlines() if ln.startswith("FROM "))
    match = re.search(r"postgres:(\d+)", from_line)
    assert match, f"unexpected base image: {from_line}"
    # 18 is the major READ from the newest dump's header and manifest on
    # 2026-09-08 (RUNBOOK.md §0), not the compose pin (17.5) and not a guess.
    assert match.group(1) == "18", (
        "the client major must match production (18, determined from the dump "
        "header — see scripts/dr/RUNBOOK.md §0)"
    )
    for tool in ("gnupg", "python3", "jq", "bash"):
        assert tool in text, f"the image needs {tool}"


def test_railway_json_declares_the_four_hourly_cron_and_the_volume() -> None:
    cfg = json.loads(RAILWAY_JSON.read_text(encoding="utf-8"))
    assert cfg["deploy"]["cronSchedule"] == "0 */4 * * *", "RPO is 4 h; the cron is the RPO"
    assert cfg["deploy"]["restartPolicyType"] == "NEVER"
    assert cfg["build"]["dockerfilePath"] == "apps/dr-backup/Dockerfile"
    mounts = [v["mountPath"] for v in cfg["volumes"]]
    assert mounts == ["/backups"]
    # Variable NAMES only — never a value.
    for name in cfg["x-crawmatic"]["variable_names_only"]:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), f"{name} looks like a value, not a name"


def test_no_secret_value_is_assigned_in_any_dr_script() -> None:
    """No script may carry a literal password/token/DSN.

    Assignments from another variable, from a file, or to the empty default
    are what these files are supposed to contain.
    """
    pattern = re.compile(
        r"""(?im)^\s*(?:export\s+)?(\w*(?:PASSWORD|PASSPHRASE|TOKEN|SECRET)\w*)=(?!["']?\$)(?!["']{2}\s*$)(["']?)([^\s"'#]+)\2""",
    )
    for path in SCANNED:
        for name, _q, value in pattern.findall(path.read_text(encoding="utf-8")):
            # Defaults that name another variable/file, or are empty, are fine.
            assert value in {"", "0", "1"} or value.startswith("$") or "/" in value, (
                f"{path.relative_to(REPO_ROOT)} assigns {name} a literal value — "
                "secrets are variable NAMES and file locations in this repo, never values"
            )
