"""One tray per user: an exclusive lock on a file, held for the life of the process."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import BinaryIO

from ..daemon.config import ensure_private_dir


class AlreadyRunning(Exception):
    pass


def acquire(path: Path) -> BinaryIO:
    """Lock ``path``; keep the returned file open to keep the lock (the OS drops it when the
    process exits, however it exits). Raises AlreadyRunning if another process holds it."""
    ensure_private_dir(path.parent)
    handle = path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning(f"qsv-clientd-tray is already running ({path} is locked)") from None
    return handle
