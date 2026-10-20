"""Install qsv-clientd as a per-user service: launchd (macOS) or systemd --user (Linux)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from ..client import find_qsv
from .config import DaemonConfig

LABEL = "com.dathere.qsv-clientd"


def _command(binary: str) -> list[str]:
    exe = shutil.which("qsv-clientd")
    base = [exe] if exe else [sys.executable, "-m", "qsv_client.daemon"]
    # --binary overrides daemon.json's `binary`, which could be a bare name the service's
    # restricted PATH can't resolve (an explicit binary also outranks QSV_BIN)
    return [*base, "serve", "--binary", binary]


def service_environment() -> dict[str, str]:
    """What the service needs from this shell, which launchd/systemd won't otherwise give it.

    Services don't read shell profiles (launchd's PATH is just /usr/bin:/bin:/usr/sbin:/sbin),
    so qsv is resolved now, the way the daemon would (daemon.json ``binary``, ``$QSV_BIN``,
    PATH), and pinned as QSV_BIN. Raises QsvNotFound if there is no qsv to pin. Other QSV_*
    settings are deliberately not copied: the service file is world-readable and they can
    hold secrets such as QSV_LLM_APIKEY.
    """
    env = {"QSV_BIN": find_qsv(DaemonConfig.load().binary)}
    for name in ("QSV_CLIENTD_HOME", "QSV_CLIENTD_SOCKET"):
        if os.environ.get(name):
            # absolute: the service has its own working directory. absolute(), not
            # abspath, keeps ".." for the OS to resolve after symlinks
            env[name] = str(Path(os.environ[name]).absolute())
    return env


def launchd_plist(
    command: list[str],
    env: dict[str, str],
    *,
    label: str = LABEL,
    keep_alive: bool = True,
    log: Path | None = None,
) -> str:
    """A launchd agent; ``log`` (stderr) defaults to ~/Library/Logs/qsv-clientd.log."""
    args = "\n".join(f"    <string>{escape(a)}</string>" for a in command)
    env_items = "\n".join(
        f"    <key>{escape(k)}</key><string>{escape(v)}</string>" for k, v in env.items()
    )
    log_path = escape(str(log or Path.home() / "Library/Logs/qsv-clientd.log"))
    keep = "<true/>" if keep_alive else "<false/>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
{env_items}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>{keep}
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
"""


def _systemd_quote(value: str) -> str:
    # systemd unescapes C-style escapes in quoted values and expands % specifiers
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_unit(command: list[str], env: dict[str, str]) -> str:
    environment = "".join(f"Environment={_systemd_quote(f'{k}={v}')}\n" for k, v in env.items())
    return f"""[Unit]
Description=qsv-clientd (run qsv for local processes and Data Schematics)

[Service]
{environment}ExecStart={shlex.join(command).replace("%", "%%")}
Restart=on-failure

[Install]
WantedBy=default.target
"""


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "qsv-clientd.service"


def install() -> str:
    """Write and load the service definition; returns a description of what was done."""
    env = service_environment()
    command = _command(env["QSV_BIN"])
    if sys.platform == "darwin":
        path = _launchd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(command, env))
        subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(path)], check=True)
        return f"installed and started launchd agent {path} (qsv: {env['QSV_BIN']})"
    if sys.platform.startswith("linux"):
        path = _systemd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(command, env))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "qsv-clientd"], check=True)
        # restart, not start: a reinstall must replace a running daemon's old settings
        subprocess.run(["systemctl", "--user", "restart", "qsv-clientd"], check=True)
        return f"installed and started systemd user unit {path} (qsv: {env['QSV_BIN']})"
    # list2cmdline quotes arguments with spaces; inside /TR "..." those quotes must be \"
    task = subprocess.list2cmdline(command).replace('"', '\\"')
    return (
        "automatic install is not supported on this platform; to start qsv-clientd at logon run:\n"
        f'  schtasks /Create /SC ONLOGON /TN qsv-clientd /TR "{task}"'
    )


def uninstall() -> str:
    if sys.platform == "darwin":
        path = _launchd_path()
        subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
        path.unlink(missing_ok=True)
        return f"removed {path}"
    if sys.platform.startswith("linux"):
        path = _systemd_path()
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "qsv-clientd"],
            check=False,
            capture_output=True,
        )
        path.unlink(missing_ok=True)
        return f"removed {path}"
    return "remove the task with: schtasks /Delete /TN qsv-clientd"


def service_installed() -> Literal["launchd", "systemd"] | None:
    """Which service ``install`` set up (by its file), or None. It may not be running."""
    if sys.platform == "darwin" and _launchd_path().exists():
        return "launchd"
    if sys.platform.startswith("linux") and _systemd_path().exists():
        return "systemd"
    return None


def _stop_command() -> tuple[list[str], str] | None:
    if sys.platform == "darwin":
        path = _launchd_path()
        return ["launchctl", "unload", str(path)], f"stopped launchd agent {path}"
    if sys.platform.startswith("linux"):
        return ["systemctl", "--user", "stop", "qsv-clientd"], (
            "stopped systemd user unit qsv-clientd"
        )
    return None


def stop_service() -> str:
    """Stop the installed service for now; it starts again at the next login.

    Killing it isn't enough: launchd's KeepAlive (and systemd's Restart=) relaunch it.
    Raises RuntimeError, with the tool's message, when launchctl or systemctl fails.
    """
    command = _stop_command()
    if command is None:
        return "no service to stop on this platform"
    argv, done = command
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"{shlex.join(argv)} failed (exit code {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return done
