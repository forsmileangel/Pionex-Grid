"""Small file helpers shared by the scheduled tasks and the dashboard server.

The 08:15 capture, the 30-minute live task and the dashboard can run at the same
time and write the same JSON files. A reader must never see half a file, and a
read-modify-write must not drop another process's update.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write a sibling temp file, then swap it in, so readers see the old or the new file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                                     suffix=".tmp", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(text)
    try:
        # Windows refuses the swap for a moment while another process is reading the target.
        for attempt in range(20):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        tmp_path.unlink(missing_ok=True)


def append_line(path: Path, line: str, max_bytes: int) -> None:
    """Append one line to a log. Past max_bytes the older half is dropped first, so a log the
    tasks write every day cannot grow without end."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        kept = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        atomic_write_text(path, "".join(kept[len(kept) // 2:]))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line if line.endswith("\n") else line + "\n")


class LockTimeout(Exception):
    pass


@contextmanager
def file_lock(path: Path, timeout: float = 30.0):
    """Hold an exclusive lock on `<path>.lock` across processes. timeout=0 tries once."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                _lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{lock_path.name} is held by another process")
                time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


if os.name == "nt":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
