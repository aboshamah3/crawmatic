#!/usr/bin/env python3
"""Token-protected read-only file server for the encrypted backup volume (C10/F21).

The ops host's third copy comes from here: `scripts/dr/backup_prod.sh --mode
pull` asks `GET /latest` for the newest set and then fetches each file. That
is the ONLY consumer, so this is deliberately the smallest thing that can do
it -- `http.server` from the stdlib, no framework, no dependency, nothing to
patch.

Security posture, stated plainly:

* **Read-only.** Only GET is routed; there is no write path to route to.
* **Bearer token**, compared with `hmac.compare_digest`, from `$DR_PULL_TOKEN`
  (name only -- the value is an owner-set Railway variable). With the variable
  unset the server refuses EVERY request: a file server for production
  backups must fail closed, never open.
* **What it serves is already encrypted.** Every `.gpg` here is AES-256 under
  a passphrase this service has (`$DR_GPG_PASSPHRASE`) and this HTTP surface
  does not: nothing served is plaintext production data.
* **Path traversal is impossible by construction**, not by filtering: the set
  and file names off the URL are matched against strict patterns and then
  `Path.resolve()`d and checked to still be inside the sets directory.
* Binds `::` so Railway's IPv6-only private network can reach it. Whether it
  ALSO gets a public domain is an owner decision at provisioning time
  (RUNBOOK §Owner gate); the private domain is the intended path.

ONE SERVICE OR TWO -- the Railway volume constraint
---------------------------------------------------
A Railway volume attaches to exactly ONE service, and a service with a
`cronSchedule` is started for each run and exits -- it cannot also hold an
HTTP listener open. Those two facts together mean the cron shape in
`railway.json` and this file server cannot both own `/backups` as separate
services. So this process can run the schedule ITSELF: with
`$DR_SCHEDULE_LOOP=1` it starts a daemon thread that runs `/app/backup.sh`
every `$DR_SCHEDULE_INTERVAL_SECONDS` (default 4 h, the same cadence as the
`0 */4 * * *` in `railway.json`) while continuing to serve. The owner picks
one shape at provisioning time; both are written out in
`scripts/dr/RUNBOOK.md` §Owner gate.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path

SETS_DIR = Path(os.environ.get("DR_ROOT", "/backups")) / "sets"
TOKEN = os.environ.get("DR_PULL_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
SET_RE = re.compile(r"^set-\d{8}T\d{6}Z$")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SCHEDULE_LOOP = os.environ.get("DR_SCHEDULE_LOOP", "") == "1"
SCHEDULE_INTERVAL = int(os.environ.get("DR_SCHEDULE_INTERVAL_SECONDS", "14400"))  # 4 h
BACKUP_CMD = os.environ.get("DR_BACKUP_CMD", "/app/backup.sh")


def schedule_loop() -> None:
    """Run the backup every SCHEDULE_INTERVAL seconds, forever.

    A failed run is logged and the loop continues: one failed backup must not
    stop every later backup. The run itself is loud on failure (`DR-ALERT` on
    stderr, non-zero exit -- see `dr_lib.sh`), which is what the ops alerting
    gate in RUNBOOK §4 hangs off.
    """
    while True:
        started = time.time()
        try:
            rc = subprocess.call(["/bin/bash", BACKUP_CMD])
            print(f"dr-backup-serve: scheduled backup exited {rc}", flush=True)
        except Exception as exc:  # never let the scheduler thread die
            print(f"dr-backup-serve: scheduled backup raised {exc!r}", flush=True)
        time.sleep(max(60.0, SCHEDULE_INTERVAL - (time.time() - started)))


def newest_set() -> Path | None:
    sets = sorted((p for p in SETS_DIR.glob("set-*") if p.is_dir() and SET_RE.match(p.name)))
    return sets[-1] if sets else None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "crawmatic-dr-backup/1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        presented = header[7:].strip() if header.startswith("Bearer ") else ""
        # No token configured => refuse everything (fail closed).
        return bool(TOKEN) and hmac.compare_digest(presented, TOKEN)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if not self._authorized():
            self._send(401, b'{"error":"unauthorized"}')
            return
        path = self.path.split("?", 1)[0]
        if path == "/latest":
            newest = newest_set()
            if newest is None:
                self._send(404, b'{"error":"no set on this volume"}')
                return
            # sha256 comes from the set's own SHA256SUMS when it exists --
            # re-hashing ~30 MB of ciphertext on every poll would make the
            # daily pull's cheapest call its most expensive one. The puller
            # verifies SHA256SUMS itself after the transfer either way.
            sums = {}
            sumfile = newest / "SHA256SUMS"
            if sumfile.is_file():
                for line in sumfile.read_text().splitlines():
                    digest, _, name = line.partition(" ")
                    sums[name.strip().lstrip("*./")] = digest.strip()
            files = [
                {
                    "name": f.name,
                    "bytes": f.stat().st_size,
                    "sha256": sums.get(f.name)
                    or hashlib.sha256(f.read_bytes()).hexdigest(),
                }
                for f in sorted(newest.iterdir())
                if f.is_file()
            ]
            self._send(200, json.dumps({"set": newest.name, "files": files}).encode())
            return
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "sets" and SET_RE.match(parts[1]) and FILE_RE.match(parts[2]):
            target = (SETS_DIR / parts[1] / parts[2]).resolve()
            if SETS_DIR.resolve() in target.parents and target.is_file():
                # Streamed, not read_bytes(): a dump is tens of megabytes and
                # this process has a container memory limit.
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.end_headers()
                with target.open("rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
                return
        self._send(404, b'{"error":"not found"}')

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log the Authorization header; the default logs the request
        # line only, which is what we want, but make the guarantee explicit.
        print("dr-backup-serve %s - %s" % (self.address_string(), fmt % args), flush=True)


class Server(socketserver.ThreadingTCPServer):
    address_family = socket.AF_INET6  # Railway's private network is IPv6-only
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    if not TOKEN:
        print("dr-backup-serve: $DR_PULL_TOKEN is unset — every request will be refused", flush=True)
    if SCHEDULE_LOOP:
        print(
            f"dr-backup-serve: $DR_SCHEDULE_LOOP=1 — running {BACKUP_CMD} "
            f"every {SCHEDULE_INTERVAL}s in-process",
            flush=True,
        )
        threading.Thread(target=schedule_loop, name="dr-backup-schedule", daemon=True).start()
    with Server(("::", PORT), Handler) as httpd:
        print(f"dr-backup-serve: listening on [::]:{PORT}, serving {SETS_DIR}", flush=True)
        httpd.serve_forever()
