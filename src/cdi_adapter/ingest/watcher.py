"""Folder-watch intake (ARCHITECTURE §3.1, TECH_STACK §5): ``cdi-ingest-watch``.

Watches ``CDI_INBOX_DIR``. A new file is waited on until its size is stable (a scanner may still be
writing it), then **moved** into ``CDI_PROCESSED_DIR`` (an atomic rename on the same volume, so the
bytes are never lost) and queued through the pipeline. Files that are not a PDF or supported
image, or that cannot be queued, are moved to ``CDI_FAILED_DIR``. Files already in the inbox at
start-up are processed too.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from cdi_adapter.config import get_settings
from cdi_adapter.ingest.pages import UnsupportedDocument, sniff_mime
from cdi_adapter.logging import configure, get_logger

log = get_logger(__name__)

STABLE_CHECKS = 3
STABLE_INTERVAL = 0.5


def _unique(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem}.{n}{Path(name).suffix}"
        n += 1
    return dest


def wait_until_stable(path: Path, checks: int = STABLE_CHECKS,
                      interval: float = STABLE_INTERVAL, timeout: float = 120) -> bool:  # fmt: skip
    last, same, t0 = -1, 0, time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        same = same + 1 if size == last and size > 0 else 0
        if same >= checks:
            return True
        last = size
        time.sleep(interval)
    return False


class InboxHandler(FileSystemEventHandler):
    def __init__(self, inbox: Path, processed: Path, failed: Path,
                 submit: Callable[[Path], object]) -> None:  # fmt: skip
        self.inbox, self.processed, self.failed, self.submit = inbox, processed, failed, submit

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.process(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:  # e.g. "save as" into the inbox
        if not event.is_directory and Path(str(event.dest_path)).parent == self.inbox:
            self.process(Path(str(event.dest_path)))

    def process(self, path: Path) -> Path | None:
        if path.name.startswith(".") or not wait_until_stable(path):
            return None
        try:
            with path.open("rb") as fh:
                sniff_mime(fh.read(16), path.name)
        except (UnsupportedDocument, OSError) as exc:
            dest = _unique(self.failed, path.name)
            shutil.move(str(path), dest)
            log.warning("folder_rejected", file=path.name, reason=str(exc), moved_to=str(dest))
            return dest
        dest = _unique(self.processed, path.name)
        shutil.move(str(path), dest)
        try:
            self.submit(dest)
        except Exception as exc:  # the broker is down: park the file where an operator sees it
            failed = _unique(self.failed, path.name)
            shutil.move(str(dest), failed)
            log.error("folder_submit_failed", file=path.name, error=str(exc), moved_to=str(failed))
            return failed
        log.info("folder_queued", file=path.name, stored_at=str(dest))
        return dest


def build_handler(submit: Callable[[Path], object] | None = None) -> InboxHandler:
    s = get_settings()
    dirs = [Path(d) for d in (s.inbox_dir, s.processed_dir, s.failed_dir)]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    if submit is None:
        from cdi_adapter.worker import submit_file

        submit = submit_file
    inbox, processed, failed = dirs
    return InboxHandler(inbox, processed, failed, submit=submit)


def main() -> None:  # pragma: no cover - long-running process
    configure()
    handler = build_handler()
    for existing in sorted(handler.inbox.iterdir()):
        if existing.is_file():
            handler.process(existing)
    observer = Observer()
    observer.schedule(handler, str(handler.inbox), recursive=False)
    observer.start()
    log.info("watching", inbox=str(handler.inbox))
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":  # pragma: no cover
    main()
