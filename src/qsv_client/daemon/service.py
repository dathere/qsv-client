"""Install qsv-clientd as a per-user service: launchd (macOS) or systemd --user (Linux)."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "com.dathere.qsv-clientd"


def _command() -> list[str]:
    exe = shutil.which("qsv-clientd")
    return [exe, "serve"] if exe else [sys.executable, "-m", "qsv_client.daemon", "serve"]


def launchd_plist(command: list[str]) -> str:
    args = "\n".join(f"    <string>{escape(a)}</string>" for a in command)
    log = escape(str(Path.home() / "Library/Logs/qsv-clientd.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def systemd_unit(command: list[str]) -> str:
    return f"""[Unit]
Description=qsv-clientd (run qsv for local processes and Data Schematics)

[Service]
ExecStart={shlex.join(command)}
Restart=on-failure

[Install]
WantedBy=default.target
"""


def install() -> str:
    """Write and load the service definition; returns a description of what was done."""
    command = _command()
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(command))
        subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(path)], check=True)
        return f"installed and started launchd agent {path}"
    if sys.platform.startswith("linux"):
        path = Path.home() / ".config" / "systemd" / "user" / "qsv-clientd.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(command))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "qsv-clientd"], check=True)
        return f"installed and started systemd user unit {path}"
    task = subprocess.list2cmdline(command)
    return (
        "automatic install is not supported on this platform; to start qsv-clientd at logon run:\n"
        f'  schtasks /Create /SC ONLOGON /TN qsv-clientd /TR "{task}"'
    )


def uninstall() -> str:
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        subprocess.run(["launchctl", "unload", str(path)], check=False, capture_output=True)
        path.unlink(missing_ok=True)
        return f"removed {path}"
    if sys.platform.startswith("linux"):
        path = Path.home() / ".config" / "systemd" / "user" / "qsv-clientd.service"
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "qsv-clientd"],
            check=False,
            capture_output=True,
        )
        path.unlink(missing_ok=True)
        return f"removed {path}"
    return "remove the task with: schtasks /Delete /TN qsv-clientd"
