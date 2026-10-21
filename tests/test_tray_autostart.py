"""Starting the tray at login on each platform. Nothing real is touched: ``Path.home`` points
into tmp_path, the platform is patched, and the Windows registry is a fake."""

from __future__ import annotations

import plistlib
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from qsv_client.daemon import service
from qsv_client.tray import autostart


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("XDG_CONFIG_HOME", "QSV_CLIENTD_HOME", "QSV_CLIENTD_SOCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/bin/qsv-clientd-tray")
    monkeypatch.setattr(autostart, "service_environment", lambda: {"QSV_BIN": "/opt/q s/qsv"})
    return home


def test_the_command_prefers_the_gui_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/bin/qsv-clientd-tray")
    assert autostart.command() == ["/opt/bin/qsv-clientd-tray"]
    monkeypatch.setattr(shutil, "which", lambda name: None)
    python = tmp_path / "python.exe"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "platform", "linux")
    assert autostart.command() == [str(python), "-m", "qsv_client.tray"]
    monkeypatch.setattr(sys, "platform", "win32")
    assert autostart.command()[0] == str(python)  # no pythonw.exe beside it
    (tmp_path / "pythonw.exe").write_text("")
    assert autostart.command() == [str(tmp_path / "pythonw.exe"), "-m", "qsv_client.tray"]


def test_macos(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    path = home / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
    assert autostart.is_enabled() is False
    assert autostart.enable() == f"the tray will start at login ({path})"
    assert autostart.is_enabled() is True
    plist = plistlib.loads(path.read_bytes())
    assert plist["Label"] == autostart.LABEL
    # --binary too: a bare `binary` in daemon.json would outrank QSV_BIN on launchd's PATH
    assert plist["ProgramArguments"] == ["/opt/bin/qsv-clientd-tray", "--binary", "/opt/q s/qsv"]
    assert plist["EnvironmentVariables"] == {"QSV_BIN": "/opt/q s/qsv"}  # launchd's PATH lacks qsv
    assert plist["KeepAlive"] is False and plist["RunAtLoad"] is True  # Quit stays quit
    assert plist["LimitLoadToSessionType"] == "Aqua"  # a GUI login only
    stderr = home / "Library" / "Logs" / "qsv-client" / "qsv-clientd-tray.stderr.log"
    assert plist["StandardErrorPath"] == str(stderr) and stderr.parent.is_dir()
    assert autostart.disable() == "the tray won't start at login"
    assert autostart.is_enabled() is False and not path.exists()
    autostart.disable()  # again: fine


def test_the_service_plist_is_unchanged() -> None:
    plist = service.launchd_plist(["/x"], {})
    assert "LimitLoadToSessionType" not in plist


def test_linux(home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    path = home / ".config" / "autostart" / "qsv-clientd-tray.desktop"
    assert autostart.is_enabled() is False
    autostart.enable()
    assert autostart.is_enabled() is True
    text = path.read_text()
    assert text.startswith("[Desktop Entry]\nType=Application\n")
    assert "\nExec=/opt/bin/qsv-clientd-tray\n" in text
    autostart.disable()
    assert not path.exists()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    autostart.enable()
    assert (tmp_path / "cfg" / "autostart" / "qsv-clientd-tray.desktop").exists()


@pytest.mark.parametrize(
    ("argv", "line"),
    [
        (["/usr/bin/qsv-clientd-tray"], "/usr/bin/qsv-clientd-tray"),
        (
            ["/opt/my apps/py$thon", "-m", "qsv_client.tray"],
            '"/opt/my apps/py\\\\$thon" -m qsv_client.tray',
        ),
        (["/a b/x\\y", "100%"], '"/a b/x\\\\\\\\y" 100%%'),
        ([""], '""'),
    ],
)
def test_desktop_exec_quoting(argv: list[str], line: str) -> None:
    assert autostart._desktop_exec(argv) == line


class FakeKey:
    def __enter__(self) -> FakeKey:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        pass


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []

    def CreateKey(self, root: str, path: str) -> FakeKey:
        self.keys.setdefault(path, {})
        return FakeKey()

    def OpenKey(self, root: str, path: str, *args: Any) -> FakeKey:
        if path not in self.keys:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        self.calls.append(("OpenKey", root, path, *args))
        return FakeKey()

    def SetValueEx(self, key: FakeKey, name: str, reserved: int, kind: int, value: str) -> None:
        self.keys[autostart._RUN_KEY][name] = (kind, value)

    def QueryValueEx(self, key: FakeKey, name: str) -> Any:
        if name not in self.keys[autostart._RUN_KEY]:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return self.keys[autostart._RUN_KEY][name]

    def DeleteValue(self, key: FakeKey, name: str) -> None:
        if name not in self.keys[autostart._RUN_KEY]:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.keys[autostart._RUN_KEY][name]


@pytest.fixture
def winreg(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWinreg]:
    fake = FakeWinreg()
    monkeypatch.setattr(autostart, "_winreg", lambda: fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda name: r"C:\Program Files\Py\qsv-clientd-tray.exe")
    yield fake


def test_windows(winreg: FakeWinreg) -> None:
    assert autostart.is_enabled() is False  # no Run key at all
    assert autostart.disable() == "the tray won't start at login"
    assert autostart.enable().endswith(r"(HKCU\Software\Microsoft\Windows\CurrentVersion\Run)")
    assert autostart.is_enabled() is True
    run = winreg.keys[autostart._RUN_KEY]
    assert run == {"qsv-clientd-tray": (1, r'"C:\Program Files\Py\qsv-clientd-tray.exe"')}
    autostart.disable()
    assert autostart.is_enabled() is False and run == {}
    assert ("OpenKey", "HKCU", autostart._RUN_KEY, 0, 2) in winreg.calls  # opened to write
    autostart.disable()  # the value is gone: fine


def test_elsewhere_it_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert autostart.is_enabled() is None
    with pytest.raises(OSError, match="isn't supported on freebsd14"):
        autostart.enable()
    with pytest.raises(OSError, match="isn't supported"):
        autostart.disable()


def test_the_real_winreg_seam() -> None:
    if sys.platform != "win32":
        pytest.skip("winreg exists only on Windows")
    assert autostart._winreg().HKEY_CURRENT_USER is not None
