"""Where qsv-clientd keeps its files on each platform, daemon.toml, and the service installer.

Nothing real is touched: ``Path.home`` points into tmp_path, ``sys.platform`` is patched to
each supported OS, and launchctl/systemctl calls are recorded instead of run.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from qsv_client.daemon import config, service
from qsv_client.daemon.config import DaemonConfig

_ENV = (
    "QSV_CLIENTD_HOME",
    "QSV_CLIENTD_SOCKET",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
)


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home directory, with none of the variables that would override it."""
    home = tmp_path / "home"
    home.mkdir()
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


# -- directories ----------------------------------------------------------------------------


def test_windows_dirs(user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert config.config_dir() == user_home / "AppData" / "Roaming" / "qsv-client"
    assert config.state_dir() == user_home / "AppData" / "Local" / "qsv-client"
    assert config.runtime_dir() == user_home / "AppData" / "Local" / "qsv-client" / "run"
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.config_dir() == tmp_path / "roaming" / "qsv-client"
    assert config.state_dir() == tmp_path / "local" / "qsv-client"


def test_macos_dirs(user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    # XDG variables are a Linux convention, even when set on a Mac
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-run"))
    support = user_home / "Library" / "Application Support" / "qsv-client"
    assert config.config_dir() == support
    assert config.state_dir() == support
    assert config.runtime_dir() == support / "run"
    assert config.default_socket_path() == support / "run" / "qsvd.sock"
    assert config.local_token_path() == support / "run" / "local-token"


def test_linux_dirs(user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert config.config_dir() == user_home / ".config" / "qsv-client"
    assert config.state_dir() == user_home / ".local" / "state" / "qsv-client"
    assert config.runtime_dir() == user_home / ".local" / "state" / "qsv-client" / "run"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert config.config_dir() == tmp_path / "cfg" / "qsv-client"
    assert config.state_dir() == tmp_path / "state" / "qsv-client"
    assert config.runtime_dir() == tmp_path / "run" / "qsv-client"
    assert config.default_socket_path() == tmp_path / "run" / "qsv-client" / "qsvd.sock"
    monkeypatch.setenv("QSV_CLIENTD_SOCKET", str(tmp_path / "s.sock"))
    assert config.default_socket_path() == tmp_path / "s.sock"
    assert DaemonConfig().socket_path() == tmp_path / "s.sock"
    assert DaemonConfig(socket=str(tmp_path / "c.sock")).socket_path() == tmp_path / "c.sock"


def test_home_override_wins_everywhere(
    user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(tmp_path / "one"))
    for platform in ("win32", "darwin", "linux"):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
        assert config.config_dir() == config.state_dir() == tmp_path / "one"
        assert config.runtime_dir() == tmp_path / "one" / "run"


def test_log_path(user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    # a subdirectory: the launchd service already logs to ~/Library/Logs/qsv-clientd.log
    assert config.log_path() == user_home / "Library" / "Logs" / "qsv-client" / "qsv-clientd.log"
    monkeypatch.setattr(sys, "platform", "linux")
    assert config.log_path() == config.state_dir() / "qsv-clientd.log"
    monkeypatch.setattr(sys, "platform", "win32")
    assert config.log_path() == config.state_dir() / "qsv-clientd.log"
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(tmp_path / "one"))
    for platform in ("win32", "darwin", "linux"):
        monkeypatch.setattr(sys, "platform", platform)
        assert config.log_path() == tmp_path / "one" / "qsv-clientd.log"


def test_socket_path_error_only_for_long_paths() -> None:
    long = "/tmp/" + "x" * 200
    assert config.socket_path_error(long, OSError("AF_UNIX path too long")) is not None
    # an errno means some other failure, whatever the message says
    assert config.socket_path_error(long, OSError(errno.ENAMETOOLONG, "path too long")) is None
    assert config.socket_path_error(long, OSError("address already in use")) is None


# -- files ----------------------------------------------------------------------------------


def test_write_private_file_leaves_nothing_behind_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "state" / "taken"
    target.mkdir(parents=True)  # os.replace can't put a file over a directory
    with pytest.raises(OSError):
        config.write_private_file(target, b"secret")
    assert target.is_dir()
    assert sorted(p.name for p in target.parent.iterdir()) == ["taken"]


def test_write_private_file_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "new" / "token"
    config.write_private_file(target, b"one")
    config.write_private_file(target, b"two")  # replaces atomically
    assert target.read_bytes() == b"two"
    assert sorted(p.name for p in target.parent.iterdir()) == ["token"]
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("text", ["port = ", "port = 1\nport = 2", "{}", "[port"])
def test_config_must_be_valid_toml(tmp_path: Path, text: str) -> None:
    path = tmp_path / "daemon.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=r"daemon\.toml: "):
        DaemonConfig.load(path)


def test_a_json_config_says_the_format_changed(tmp_path: Path) -> None:
    path = tmp_path / "old.json"
    path.write_text('{"port": 0}')
    with pytest.raises(ValueError, match=r"old\.json: the config is TOML now \(daemon\.toml"):
        DaemonConfig.load(path)


def test_config_rejects_unknown_and_mistyped_settings(tmp_path: Path) -> None:
    path = tmp_path / "daemon.toml"
    path.write_text("port = 1\nprot = 2\n")
    with pytest.raises(ValueError, match=r"unknown setting.*prot"):
        DaemonConfig.load(path)
    for bad in ("port = true", 'port = "1"', 'browser_origins = ["null", 1]', "[port]"):
        path.write_text(bad)
        with pytest.raises(ValueError, match="must be"):
            DaemonConfig.load(path)


def test_config_defaults_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "daemon.toml"
    assert DaemonConfig.load() == DaemonConfig()
    (tmp_path / "daemon.toml").write_text("port = 0\n")
    assert DaemonConfig.load().port == 0


def test_the_config_template_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "daemon.toml"
    path.write_text(config.config_template())
    assert DaemonConfig.load(path) == DaemonConfig()


def test_every_setting_in_the_template_is_valid_uncommented(tmp_path: Path) -> None:
    template = config.config_template()
    assert set(config._HELP) == {f.name for f in fields(DaemonConfig)}
    settings = [
        line.removeprefix("# ")
        for line in template.splitlines()
        if line.startswith("# ") and " = " in line and line[2:].split(" = ")[0].isidentifier()
    ]
    assert [line.split(" = ")[0] for line in settings] == [f.name for f in fields(DaemonConfig)]
    path = tmp_path / "daemon.toml"
    path.write_text("\n".join(settings))
    loaded = DaemonConfig.load(path)
    defaults = DaemonConfig()
    for f in fields(DaemonConfig):
        expected = getattr(defaults, f.name)
        if expected is None:  # no default to show: the template gives an example instead
            assert getattr(loaded, f.name) == json.loads(config._HELP[f.name][1] or "")
        else:
            assert getattr(loaded, f.name) == expected


# -- service install/uninstall --------------------------------------------------------------


class _Calls:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.argv.append(list(args))
        self.kwargs.append(kwargs)
        return subprocess.CompletedProcess(args, 0, b"", b"")


@pytest.fixture
def calls(user_home: Path, monkeypatch: pytest.MonkeyPatch) -> _Calls:
    recorder = _Calls()
    monkeypatch.setattr(subprocess, "run", recorder)
    qsv = "/opt/q s/qsv"
    monkeypatch.setattr(service, "service_environment", lambda: {"QSV_BIN": qsv})
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/bin/qsv-clientd")
    return recorder


def test_install_on_macos(calls: _Calls, user_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import plistlib

    monkeypatch.setattr(sys, "platform", "darwin")
    message = service.install()
    plist_path = user_home / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    command = ["/opt/bin/qsv-clientd", "serve", "--binary", "/opt/q s/qsv"]
    assert plist["Label"] == service.LABEL
    assert plist["ProgramArguments"] == command
    assert plist["EnvironmentVariables"] == {"QSV_BIN": "/opt/q s/qsv"}
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["StandardErrorPath"] == str(user_home / "Library/Logs/qsv-clientd.log")
    # a reinstall unloads the old agent first (which may not be loaded: not checked)
    assert calls.argv == [
        ["launchctl", "unload", str(plist_path)],
        ["launchctl", "load", "-w", str(plist_path)],
    ]
    assert calls.kwargs[0]["check"] is False and calls.kwargs[1]["check"] is True
    assert str(plist_path) in message and "/opt/q s/qsv" in message


def test_install_elsewhere_prints_the_task_command(
    calls: _Calls, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(service, "service_environment", lambda: {"QSV_BIN": r"C:\qsv\qsv.exe"})
    message = service.install()
    assert calls.argv == []
    assert list(user_home.iterdir()) == []
    assert message.endswith(
        "\n  schtasks /Create /SC ONLOGON /TN qsv-clientd "
        '/TR "/opt/bin/qsv-clientd serve --binary C:\\qsv\\qsv.exe"'
    )


def test_task_command_escapes_quoted_paths(
    calls: _Calls, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        service, "service_environment", lambda: {"QSV_BIN": r"C:\Program Files\qsv\qsv.exe"}
    )
    # a quote nested in /TR "..." must be \" or it ends the /TR value early
    assert service.install().endswith(
        '/TR "/opt/bin/qsv-clientd serve --binary \\"C:\\Program Files\\qsv\\qsv.exe\\""'
    )


def test_install_without_the_entry_point_runs_the_module(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert service._command("/q") == [
        sys.executable,
        "-m",
        "qsv_client.daemon",
        "serve",
        "--binary",
        "/q",
    ]


def test_uninstall_on_macos(
    calls: _Calls, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    plist_path = user_home / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("<plist/>")
    assert service.uninstall() == f"removed {plist_path}"
    assert not plist_path.exists()
    assert calls.argv == [["launchctl", "unload", str(plist_path)]]
    assert calls.kwargs[0]["check"] is False
    # again, with nothing installed: still fine
    assert service.uninstall() == f"removed {plist_path}"


def test_uninstall_on_linux(
    calls: _Calls, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    unit = user_home / ".config" / "systemd" / "user" / "qsv-clientd.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    assert service.uninstall() == f"removed {unit}"
    assert not unit.exists()
    assert calls.argv == [["systemctl", "--user", "disable", "--now", "qsv-clientd"]]
    assert calls.kwargs[0]["check"] is False
    assert service.uninstall() == f"removed {unit}"


def test_uninstall_elsewhere_prints_the_task_command(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert service.uninstall() == "remove the task with: schtasks /Delete /TN qsv-clientd"
    assert calls.argv == []


def test_launchd_plist_options(user_home: Path) -> None:
    import plistlib

    plist = plistlib.loads(
        service.launchd_plist(
            ["/bin/tray"],
            {},
            label="com.example.<tray>",
            keep_alive=False,
            log=user_home / "tray & co.log",
        ).encode()
    )
    assert plist["Label"] == "com.example.<tray>"
    assert plist["KeepAlive"] is False and plist["RunAtLoad"] is True
    assert plist["StandardErrorPath"] == str(user_home / "tray & co.log")


@pytest.mark.parametrize(
    ("platform", "kind"), [("darwin", "launchd"), ("linux", "systemd"), ("win32", None)]
)
def test_service_installed(
    user_home: Path, monkeypatch: pytest.MonkeyPatch, platform: str, kind: str | None
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert service.service_installed() is None
    for path in (
        user_home / "Library" / "LaunchAgents" / f"{service.LABEL}.plist",
        user_home / ".config" / "systemd" / "user" / "qsv-clientd.service",
    ):
        path.parent.mkdir(parents=True)
        path.write_text("")
    assert service.service_installed() == kind


def test_stop_service(calls: _Calls, user_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plist_path = user_home / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert service.stop_service() == f"stopped launchd agent {plist_path}"
    monkeypatch.setattr(sys, "platform", "linux")
    assert service.stop_service() == "stopped systemd user unit qsv-clientd"
    monkeypatch.setattr(sys, "platform", "win32")
    assert service.stop_service() == "no service to stop on this platform"
    # unload, not kill: launchd's KeepAlive would relaunch a killed daemon
    assert calls.argv == [
        ["launchctl", "unload", str(plist_path)],
        ["systemctl", "--user", "stop", "qsv-clientd"],
    ]
    assert all(k["check"] is False for k in calls.kwargs)


@pytest.mark.parametrize(
    ("stdout", "stderr", "detail"),
    [
        ("", "Failed to connect to bus\n", ": Failed to connect to bus"),
        ("said so", "", ": said so"),
        ("", "", ""),
    ],
)
def test_stop_service_reports_a_failure(
    user_home: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str, detail: str
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout, stderr),
    )
    message = f"systemctl --user stop qsv-clientd failed (exit code 1){detail}"
    with pytest.raises(RuntimeError) as info:
        service.stop_service()
    assert str(info.value) == message


def test_service_environment_copies_only_daemon_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "find_qsv", lambda binary: "/opt/qsv")
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(tmp_path))
    monkeypatch.setenv("QSV_CLIENTD_SOCKET", str(tmp_path / "s.sock"))
    monkeypatch.setenv("QSV_LLM_APIKEY", "secret")  # never written to the service file
    assert service.service_environment() == {
        "QSV_BIN": "/opt/qsv",
        "QSV_CLIENTD_HOME": str(tmp_path),
        "QSV_CLIENTD_SOCKET": str(tmp_path / "s.sock"),
    }
    monkeypatch.setenv("QSV_CLIENTD_SOCKET", "")  # empty means unset
    assert "QSV_CLIENTD_SOCKET" not in service.service_environment()
    assert os.environ["QSV_LLM_APIKEY"] == "secret"
