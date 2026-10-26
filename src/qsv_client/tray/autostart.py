"""Start the tray at login: a launchd agent (macOS), an XDG autostart entry (Linux), or the
Run registry key (Windows). Independent of ``qsv-clientd install``: with both, the tray
attaches to the service at login.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..daemon.config import log_path
from ..daemon.service import launchd_plist, service_environment

LABEL = "com.dathere.qsv-clientd-tray"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "qsv-clientd-tray"
# characters that make an Exec argument need quotes (Desktop Entry Specification)
_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def command() -> list[str]:
    """How to launch the tray: its gui-script if installed, else the module (with pythonw on
    Windows, so no console window opens)."""
    exe = shutil.which("qsv-clientd-tray")
    if exe:
        return [exe]
    python = Path(sys.executable)
    if sys.platform == "win32" and (pythonw := python.with_name("pythonw.exe")).exists():
        python = pythonw
    return [str(python), "-m", "qsv_client.tray"]


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _desktop_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "autostart" / "qsv-clientd-tray.desktop"


def _winreg() -> Any:
    import winreg

    return winreg


def _desktop_exec(argv: list[str]) -> str:
    """An Exec value: arguments quoted per the spec, then escaped as a string value."""
    parts = []
    for arg in argv:
        arg = arg.replace("%", "%%")
        if not arg or _EXEC_RESERVED & set(arg):
            arg = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
        parts.append(arg)
    # a string value's own escaping comes on top: a quoted literal backslash is four of them
    return " ".join(parts).replace("\\", "\\\\")


def desktop_entry(argv: list[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=qsv-clientd tray\n"
        "Comment=Run and control qsv-clientd from the system tray\n"
        f"Exec={_desktop_exec(argv)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def is_enabled() -> bool | None:
    """Whether the tray starts at login; None where this can't be set up."""
    if sys.platform == "darwin":
        return _plist_path().exists()
    if sys.platform.startswith("linux"):
        return _desktop_path().exists()
    if sys.platform == "win32":
        reg = _winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                reg.QueryValueEx(key, _RUN_VALUE)
        except FileNotFoundError:
            return False
        return True
    return None


def enable() -> str:
    """Start the tray at login (from the next one); returns what was done."""
    if sys.platform == "darwin":
        # launchd's PATH can't find qsv: pin it, as `qsv-clientd install` does
        env = service_environment()
        stderr = log_path().with_name("qsv-clientd-tray.stderr.log")
        stderr.parent.mkdir(parents=True, exist_ok=True)
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # not kept alive, so Quit stays quit; written, not loaded, or a second tray starts now
        # and pass it as --binary too: daemon.toml's `binary` may be a bare name, which would
        # outrank QSV_BIN and which launchd's PATH can't resolve (as `qsv-clientd install`)
        argv = [*command(), "--binary", env["QSV_BIN"]]
        path.write_text(
            launchd_plist(argv, env, label=LABEL, keep_alive=False, log=stderr, session_type="Aqua")
        )
        return f"the tray will start at login ({path})"
    if sys.platform.startswith("linux"):
        path = _desktop_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desktop_entry(command()))
        return f"the tray will start at login ({path})"
    if sys.platform == "win32":
        reg = _winreg()
        with reg.CreateKey(reg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            reg.SetValueEx(key, _RUN_VALUE, 0, reg.REG_SZ, subprocess.list2cmdline(command()))
        return "the tray will start at login (HKCU\\" + _RUN_KEY + ")"
    raise OSError(f"starting the tray at login isn't supported on {sys.platform}")


def disable() -> str:
    """Stop starting the tray at login; fine if it wasn't set up."""
    if sys.platform == "darwin":
        _plist_path().unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        _desktop_path().unlink(missing_ok=True)
    elif sys.platform == "win32":
        reg = _winreg()
        with (
            contextlib.suppress(FileNotFoundError),
            reg.OpenKey(reg.HKEY_CURRENT_USER, _RUN_KEY, 0, reg.KEY_SET_VALUE) as key,
        ):
            reg.DeleteValue(key, _RUN_VALUE)
    else:
        raise OSError(f"starting the tray at login isn't supported on {sys.platform}")
    return "the tray won't start at login"
