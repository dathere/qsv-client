"""Open a file in the user's usual app: the tray's Open config… and Open log…."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path


def _launch(argv: list[str]) -> None:
    # detached, and reaped in the background: the opener exits at once, and must not linger
    # as a zombie until the tray quits
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )
    threading.Thread(target=proc.wait, name="qsv-tray-open", daemon=True).start()


def open_path(path: Path) -> None:
    """Open ``path`` in its app (macOS: a text editor). Raises OSError if nothing can."""
    if sys.platform == "win32":
        try:
            os.startfile(path)
        except OSError:  # most often: no app associated with .json or .log
            _launch(["notepad.exe", str(path)])
    elif sys.platform == "darwin":
        _launch(["open", "-t", str(path)])
    else:
        _launch(["xdg-open", str(path)])
