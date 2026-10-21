"""Give the tray a console that has no window (Windows).

The tray runs under pythonw, without a console, so each qsv it starts would open a console
window of its own, and ``CTRL_BREAK_EVENT``, which reaches only processes sharing the sender's
console, couldn't stop a run politely. ``AllocConsole`` would open a window (with Windows
Terminal as the default terminal, one that can't be hidden). Instead a helper is started with
``CREATE_NO_WINDOW``, which gives it a console without a window, and the tray attaches to that
console; the qsv runs then share it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any

log = logging.getLogger("qsv_client.tray")

_CREATE_NO_WINDOW = 0x08000000


def _kernel32() -> Any:
    if sys.platform == "win32":
        import ctypes

        return ctypes.WinDLL("kernel32", use_last_error=True)
    raise OSError("kernel32 exists only on Windows")


def attach_hidden() -> bool:
    """Attach to a console without a window if this process has no console; True if it did."""
    if sys.platform != "win32":
        return False
    kernel32 = _kernel32()
    if kernel32.GetConsoleCP():  # it has one already: started from a terminal
        return False
    try:
        # it waits on stdin until killed; "ready" means its console exists
        helper = subprocess.Popen(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/q", "/c", "echo ready&pause>nul"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        log.warning("no hidden console (%s): each qsv run may open a console window", exc)
        return False
    try:
        assert helper.stdout is not None
        helper.stdout.readline()
        attached = bool(kernel32.AttachConsole(helper.pid))
    finally:
        # the console lives on while the tray is attached to it
        helper.kill()
        helper.communicate()
    if not attached:
        log.warning(
            "no hidden console (AttachConsole failed): each qsv run may open a console window"
        )
    return attached
