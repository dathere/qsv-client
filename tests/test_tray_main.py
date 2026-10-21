"""The qsv-clientd-tray command and its single-instance lock (no GUI: app.run is faked)."""

from __future__ import annotations

import ctypes
import functools
import logging
import shutil
import sys
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from qsv_client.daemon import logs
from qsv_client.errors import QsvNotFound
from qsv_client.tray import __main__ as cli
from qsv_client.tray import _console, _lock, app, autostart
from qsv_client.tray.controller import Controller


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="qtm"))
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(path))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The command's messages to the user (on Windows a real MessageBoxW would block)."""
    messages: list[str] = []
    monkeypatch.setattr(cli, "_message_box", messages.append)
    return messages


class Recorder:
    def __init__(self) -> None:
        self.runs: list[Controller] = []
        self.logging: list[tuple[int, Path | None]] = []
        self.shutdowns = 0
        self.consoles: list[int] = []
        """For each attach_hidden call, how many runs had started."""
        self.raise_in_run: Exception | None = None


@pytest.fixture
def fake_gui(home: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(app, "check", lambda: None)

    def run(controller: Controller) -> None:
        rec.runs.append(controller)
        if rec.raise_in_run is not None:
            raise rec.raise_in_run

    monkeypatch.setattr(app, "run", run)
    monkeypatch.setattr(_console, "attach_hidden", lambda: rec.consoles.append(len(rec.runs)))
    monkeypatch.setattr(
        logs, "configure_logging", lambda level, file=None: rec.logging.append((level, file))
    )
    real_shutdown = Controller.shutdown

    def shutdown(self: Controller) -> None:
        rec.shutdowns += 1
        real_shutdown(self)

    monkeypatch.setattr(Controller, "shutdown", shutdown)
    return rec


def test_runs_the_tray_and_shuts_down(fake_gui: Recorder, home: Path) -> None:
    assert cli.main([]) == 0
    assert len(fake_gui.runs) == 1 and fake_gui.shutdowns == 1
    assert fake_gui.logging == [(logging.INFO, home / "qsv-clientd.log")]
    assert fake_gui.consoles == [0]  # before the controller could start qsv
    # the lock is released: the next launch runs too
    assert cli.main(["-v"]) == 0
    assert fake_gui.logging[-1] == (logging.DEBUG, home / "qsv-clientd.log")


def test_a_second_tray_is_refused(fake_gui: Recorder, home: Path, shown: list[str]) -> None:
    held = _lock.acquire(home / "run" / "tray.lock")
    try:
        assert cli.main([]) == 1
    finally:
        held.close()
    (message,) = shown
    assert message.startswith("qsv-clientd-tray is already running")
    assert fake_gui.runs == []


def test_a_crash_is_reported_and_still_shuts_down(
    fake_gui: Recorder, shown: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    fake_gui.raise_in_run = RuntimeError("no display")
    assert cli.main([]) == 1
    assert fake_gui.shutdowns == 1
    (message,) = shown
    assert message.startswith("the tray failed: no display (see ")
    assert "the tray failed" in caplog.text


def test_without_the_extra(home: Path, monkeypatch: pytest.MonkeyPatch, shown: list[str]) -> None:
    monkeypatch.setitem(sys.modules, "pystray", None)  # `import pystray` raises ImportError
    assert cli.main([]) == 1
    (message,) = shown
    assert "pip install 'qsv-client[tray]'" in message


def test_message_box_writes_to_stderr_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    cli._message_box("hello")
    assert capsys.readouterr().err == "qsv-clientd-tray: hello\n"


def test_message_box_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[tuple[Any, ...]] = []
    user32 = types.SimpleNamespace(MessageBoxW=lambda *args: shown.append(args))
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    cli._message_box("hello")
    assert shown == [(None, "hello", "qsv-clientd-tray", 0x10)]


def test_message_box_without_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stderr", None)
    cli._message_box("nowhere to say it")  # pythonw: nothing to write to, and no crash


def test_lock_is_exclusive_until_closed(home: Path) -> None:
    path = home / "new" / "tray.lock"
    first = _lock.acquire(path)
    with pytest.raises(_lock.AlreadyRunning, match="already running"):
        _lock.acquire(path)
    first.close()
    _lock.acquire(path).close()


@pytest.mark.parametrize(
    ("flag", "result"),
    [("--enable-autostart", "enabled it"), ("--disable-autostart", "disabled it")],
)
def test_autostart_flags_work_without_the_extra_or_the_lock(
    home: Path, monkeypatch: pytest.MonkeyPatch, shown: list[str], flag: str, result: str
) -> None:
    monkeypatch.setattr(autostart, "enable", lambda: "enabled it")
    monkeypatch.setattr(autostart, "disable", lambda: "disabled it")
    monkeypatch.setitem(sys.modules, "pystray", None)  # no extra
    held = _lock.acquire(home / "run" / "tray.lock")  # a tray is running
    try:
        assert cli.main([flag]) == 0
    finally:
        held.close()
    assert shown == [result]


def test_an_autostart_flag_that_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch, shown: list[str]
) -> None:
    def no_qsv() -> str:
        raise QsvNotFound("qsv not found on PATH")

    monkeypatch.setattr(autostart, "enable", no_qsv)
    assert cli.main(["--enable-autostart"]) == 1
    assert shown == ["couldn't change starting at login: qsv not found on PATH"]


def test_the_autostart_flags_are_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--enable-autostart", "--disable-autostart"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_binary_overrides_daemon_json(fake_gui: Recorder) -> None:
    assert cli.main(["--binary", "/opt/q s/qsv"]) == 0
    make = fake_gui.runs[0]._make_daemon
    assert isinstance(make, functools.partial) and make.args == ("/opt/q s/qsv",)
    assert cli.main([]) == 0
    assert fake_gui.runs[1]._make_daemon.args == (None,)  # type: ignore[attr-defined]
