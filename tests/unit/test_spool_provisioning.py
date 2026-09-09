"""The result spool's directory has to EXIST and be WRITABLE (review R10).

`Settings.SCRAPE_RESULT_SPOOL_PATH` defaults to
`/var/lib/crawmatic/spool/scrape_results.sqlite3`, and
`BatchedPersistencePipeline.__init__` opens a `ResultSpool` over it
unconditionally — so the spool directory is a hard precondition of
*constructing the pipeline*, which Scrapy does before a spider has
fetched anything. Both scraper images `chown -R app:app /app` and drop to
`USER app` without ever creating `/var/lib/crawmatic/spool`, and compose
mounted no volume there, so on those images every new spider died during
pipeline construction with a bare `PermissionError` naming a path nobody
had ever configured.

Three separate things have to be true, and each is its own way to get it
wrong, so each is its own test here:

1. **the failure is legible.** A `PermissionError: [Errno 13]` on a path
   that appears in no compose file and no Dockerfile is an hour of
   someone's evening. `ResultSpool` now fails fast with a message that
   names the directory, the setting, and the env var that overrides it.
2. **the happy path is really durable across a restart.** The spool
   exists so a killed container replays what it had already fetched, so
   the writable case is tested by re-opening the SAME file from a second
   pipeline instance and draining it — not by asserting the constructor
   returned.
3. **the images and the compose file actually provision it.** Tests 1 and
   2 would both pass on an image that still cannot write there. These are
   text assertions over the Dockerfiles and `docker-compose.yml` (no
   image is built here — the packet forbids it), which is a weaker
   statement than running the container and is labelled as such.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from twisted.internet.defer import Deferred, fail as defer_fail, succeed
from twisted.internet.task import Clock
from twisted.python.failure import Failure

from app_shared.config import Settings
from app_shared.enums import AccessMethod, ExtractionMethod, StockStatus

from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.pipelines import BatchedPersistencePipeline
from scrape_core.result_spool import (
    SPOOL_PATH_SETTING,
    ResultSpool,
    SpoolNotWritableError,
    configured_spool_path,
    main as spool_preflight_main,
    preflight as spool_preflight,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ID = uuid.uuid4()


def _make_result() -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=uuid.uuid4(),
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=True,
        price=Decimal("9.99"),
        currency="USD",
        stock_status=StockStatus.IN_STOCK,
        extraction_method=ExtractionMethod.JSON_LD,
        extraction_confidence=Decimal("0.9500"),
    )


class _SyncRunInThread:
    """`run_in_thread` without a reactor — the `test_pipeline_flush_retry` seam."""

    def __call__(self, fn: Any, *args: Any, **kwargs: Any) -> Deferred:
        try:
            return succeed(fn(*args, **kwargs))
        except Exception:  # noqa: BLE001 - mirrors deferToThread's error path
            return defer_fail(Failure())


def _pipeline(spool_path: Path, clock: Clock) -> BatchedPersistencePipeline:
    return BatchedPersistencePipeline(
        max_items=50,
        interval_seconds=60.0,
        spool_path=spool_path,
        max_pending_batches=8,
        retry_backoff_seconds=(1.0,),
        quarantine_after=5,
        clock=clock,
    )


# --- 1. the failure is legible ----------------------------------------------


class TestUnwritableSpoolFailsFast:
    def test_pipeline_construction_names_the_path_and_the_override(
        self, tmp_path: Path
    ) -> None:
        """The R10 failure mode, reproduced at its real seam.

        A directory that cannot be created is the container case exactly:
        `/var/lib/crawmatic` is root-owned and the spider runs as `app`.
        Reproduced here with a parent that is a regular FILE, so the
        `mkdir` fails identically for an unprivileged user and for root
        (the suite runs as both, depending on the host).
        """
        blocker = tmp_path / "not-a-directory"
        blocker.write_bytes(b"")
        spool_path = blocker / "spool" / "scrape_results.sqlite3"

        with pytest.raises(SpoolNotWritableError) as excinfo:
            _pipeline(spool_path, Clock())

        message = str(excinfo.value)
        assert str(spool_path.parent) in message, "the message must name the directory"
        assert SPOOL_PATH_SETTING in message, (
            "the message must name the setting/env var that moves the spool, or the "
            "operator has a path and no lever"
        )
        assert "scrapers" in message.lower() or "volume" in message.lower(), (
            "the message must say what to DO about it"
        )

    @pytest.mark.skipif(
        __import__("os").geteuid() == 0,
        reason="root ignores directory permissions; the mkdir case above covers both",
    )
    def test_an_existing_but_read_only_directory_is_refused_too(
        self, tmp_path: Path
    ) -> None:
        """`exist_ok=True` is not `writable`.

        The container failure could equally be a spool directory that a
        volume mount created root-owned: `mkdir(exist_ok=True)` succeeds
        and the very next `sqlite3.connect` fails. The check has to probe
        writability, not existence.
        """
        directory = tmp_path / "readonly"
        directory.mkdir(mode=0o500)

        with pytest.raises(SpoolNotWritableError) as excinfo:
            ResultSpool(directory / "scrape_results.sqlite3")

        assert str(directory) in str(excinfo.value)

    def test_the_error_is_raised_before_any_file_is_created(self, tmp_path: Path) -> None:
        """A fail-fast that half-succeeded would be worse than none."""
        blocker = tmp_path / "blocker"
        blocker.write_bytes(b"")

        with pytest.raises(SpoolNotWritableError):
            ResultSpool(blocker / "spool" / "scrape_results.sqlite3")

        assert blocker.is_file(), "nothing under the blocked path may have been created"


# --- 2. the writable path survives a restart --------------------------------


def test_a_writable_spool_replays_across_a_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The point of the spool: a killed container drains what it had.

    Pipeline A accepts an item and its flush fails, so the row stays
    spooled. Pipeline A is then abandoned entirely (the container died)
    and pipeline B is constructed over the SAME file — a restart, not a
    retry — and its `open_spider` replay drains the row that pipeline A
    had already been paid for.
    """
    spool_path = tmp_path / "spool" / "scrape_results.sqlite3"
    flushed: list[int] = []

    def ok_flush(workspace_id: Any, batch: list[ScrapeResult], spool_ids: Any) -> None:
        flushed.append(len(batch))

    monkeypatch.setattr(pipelines_mod, "run_in_thread", _SyncRunInThread())

    # `max_items=50`, so this item is written to the spool and is still
    # sitting in the in-memory buffer when the process is killed — the
    # exact window the spool exists for.
    dying = _pipeline(spool_path, Clock())
    dying.process_item(_make_result(), spider=None)

    assert spool_path.exists(), "the spool directory was created on the happy path"
    assert dying.spool.pending_count() == 1
    del dying  # the container is gone; nothing in memory survives

    monkeypatch.setattr(pipelines_mod, "_flush_batch", ok_flush)
    restarted = _pipeline(spool_path, Clock())
    assert restarted.spool.pending_count() == 1, "the new process sees the old row"

    restarted.replay_pending()

    assert flushed == [1]
    assert restarted.spool.pending_count() == 0


def test_the_default_spool_path_is_the_one_the_images_provision() -> None:
    """The three places that must agree on one directory.

    `Settings` names the default, and the Dockerfile/compose assertions
    below are written against `SPOOL_DIRECTORY` rather than a re-typed
    literal, so moving the default in config.py cannot leave the image
    provisioning a directory nothing uses.
    """
    assert Settings.model_fields["SCRAPE_RESULT_SPOOL_PATH"].default == Path(
        "/var/lib/crawmatic/spool/scrape_results.sqlite3"
    )


SPOOL_DIRECTORY = str(
    Settings.model_fields["SCRAPE_RESULT_SPOOL_PATH"].default.parent
)


# --- 3. the images and compose provision it ---------------------------------


@pytest.mark.parametrize(
    "dockerfile",
    ["apps/scrapers/Dockerfile", "apps/scrapers-browser/Dockerfile"],
)
def test_the_scraper_images_create_the_spool_directory_before_dropping_privileges(
    dockerfile: str,
) -> None:
    """A STATIC read of the Dockerfile — no image is built here.

    The packet forbids building the images, so this asserts the text: a
    `mkdir -p <spool dir>` and a `chown` naming the runtime user appear
    BEFORE the `USER app` line. That is weaker than running the
    container; it is the strongest check available without a build, and
    it is the one that would have caught R10, whose whole shape was
    "the directory is never mentioned anywhere".
    """
    text = (REPO_ROOT / dockerfile).read_text()
    lines = text.splitlines()

    user_line = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("USER app")),
        None,
    )
    assert user_line is not None, f"{dockerfile} no longer drops to USER app"

    before = "\n".join(lines[:user_line])
    assert SPOOL_DIRECTORY in before, (
        f"{dockerfile} never mentions {SPOOL_DIRECTORY}; a spider on this image dies "
        "during pipeline construction"
    )
    mkdir = re.search(rf"mkdir\s+-p\s+[^\n]*{re.escape(SPOOL_DIRECTORY)}", before)
    assert mkdir is not None, f"{dockerfile} must `mkdir -p {SPOOL_DIRECTORY}`"
    # The chown may name the spool directory itself or any ancestor of it
    # (the images chown `/var/lib/crawmatic`, covering the netledger
    # buffer's sibling directory in the same layer).
    ancestors = [SPOOL_DIRECTORY, *(str(p) for p in Path(SPOOL_DIRECTORY).parents)]
    chown = any(
        re.search(rf"chown[^\n]*app:app[^\n]*{re.escape(candidate)}(\s|$|\\)", before)
        for candidate in ancestors
        if candidate not in ("/", ".")
    )
    assert chown, (
        f"{dockerfile} creates {SPOOL_DIRECTORY} but leaves it root-owned; the spider "
        "runs as `app`"
    )


@pytest.mark.parametrize("service", ["scrapers", "scrapers-browser"])
def test_compose_gives_each_scraper_service_a_spool_volume(service: str) -> None:
    """The spool must outlive the container, or it is not a spool.

    A directory created in the image is writable but ephemeral: a
    container restart is exactly the event the spool exists to survive,
    and an image-layer directory is discarded with the container. Parsed
    with PyYAML (not grepped) so an indentation change cannot make this
    pass by accident.
    """
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())

    volumes = compose["services"][service].get("volumes") or []
    mounts = [v.split(":")[1] if isinstance(v, str) else v.get("target") for v in volumes]
    # The mount may target the spool directory or any ancestor of it: the
    # services mount `/var/lib/crawmatic`, which carries the netledger
    # buffer's sibling directory on the same volume.
    covered = {SPOOL_DIRECTORY, *(str(parent) for parent in Path(SPOOL_DIRECTORY).parents)}
    assert covered & set(mounts), (
        f"compose service {service!r} mounts nothing covering {SPOOL_DIRECTORY} "
        f"(mounts: {mounts}): the spool would be discarded with the container it "
        "exists to survive"
    )

    named = [v.split(":")[0] for v in volumes if isinstance(v, str)]
    declared = compose.get("volumes") or {}
    for name in named:
        if not name.startswith((".", "/")):
            assert name in declared, f"volume {name!r} is used but never declared"


# --- 4. the daemon refuses to start when it cannot spool --------------------
#
# Tests 1-3 leave one hole: an image that provisions the directory correctly
# and a VOLUME that mounts over it root-owned (Railway attaches volumes as
# root; compose copies the image's ownership only when the volume is empty).
# In that world the Dockerfile text is right, the compose text is right, and
# every spider still dies in `BatchedPersistencePipeline.__init__` — one
# `[Errno 13]` per per-job log, on a daemon whose own health check is green.
# So the writability check also runs at container start, before scrapyd is
# exec'd, where a failure is a container that will not come up.


def test_preflight_accepts_a_writable_directory(tmp_path: Path) -> None:
    target = tmp_path / "spool" / "scrape_results.sqlite3"

    checked = spool_preflight(target)

    assert checked == target
    assert target.parent.is_dir(), "the preflight provisions the directory it checks"
    assert not target.exists(), "the preflight must not create the spool FILE"


def test_preflight_refuses_an_unwritable_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(b"")

    with pytest.raises(SpoolNotWritableError):
        spool_preflight(blocker / "spool" / "scrape_results.sqlite3")


def test_preflight_honours_the_env_override_and_otherwise_the_settings_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The command takes no arguments in the entrypoints, so the path it
    checks must be the same one the pipeline will open: the env var if set,
    the `Settings` field default otherwise. Read off `model_fields` rather
    than by instantiating `Settings`, so a container missing an unrelated
    required env var still gets the spool answer."""
    monkeypatch.delenv(SPOOL_PATH_SETTING, raising=False)
    assert configured_spool_path() == Settings.model_fields[SPOOL_PATH_SETTING].default

    override = tmp_path / "elsewhere" / "scrape_results.sqlite3"
    monkeypatch.setenv(SPOOL_PATH_SETTING, str(override))
    assert configured_spool_path() == override


def test_preflight_command_exits_nonzero_and_says_why(
    tmp_path: Path, capsys: Any
) -> None:
    """`python -m scrape_core.result_spool` is what the entrypoints run, so
    the contract that matters is the EXIT STATUS (`set -eu` turns it into a
    refusal to start) plus a message on stderr naming the directory."""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")

    assert spool_preflight_main([str(blocker / "spool" / "scrape_results.sqlite3")]) == 1
    captured = capsys.readouterr()
    assert str(blocker / "spool") in captured.err
    assert SPOOL_PATH_SETTING in captured.err

    good = tmp_path / "spool" / "scrape_results.sqlite3"
    assert spool_preflight_main([str(good)]) == 0
    assert "ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    "entrypoint",
    ["apps/scrapers/docker-entrypoint.sh", "apps/scrapers-browser/docker-entrypoint.sh"],
)
def test_both_entrypoints_preflight_the_spool_before_exec(entrypoint: str) -> None:
    """A static read of the shell script, for the same reason as the
    Dockerfile assertions above: no container is started here. The two facts
    that matter are that the check runs at all, and that it runs BEFORE
    `exec "$@"` — after the exec it would never run, since exec replaces the
    process."""
    text = (REPO_ROOT / entrypoint).read_text()

    assert text.lstrip().startswith("#!"), entrypoint
    assert "set -eu" in text, (
        f"{entrypoint} must keep `set -eu`, or a failing preflight would be ignored "
        "and scrapyd would start anyway"
    )

    check = text.find("python -m scrape_core.result_spool")
    assert check != -1, f"{entrypoint} never preflights the spool"
    exec_line = text.find('exec "$@"')
    assert exec_line != -1 and check < exec_line, (
        f"{entrypoint} preflights the spool after `exec`, where it never runs"
    )
