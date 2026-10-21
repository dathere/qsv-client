"""The tray's hidden console on Windows: faked everywhere, and for real on Windows."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from qsv_client.tray import _console


class FakeKernel32:
    def __init__(self, cp: int = 0, attaches: bool = True) -> None:
        self.cp, self.attaches = cp, attaches
        self.events: list[str] = []

    def GetConsoleCP(self) -> int:
        return self.cp

    def AttachConsole(self, pid: int) -> int:
        self.events.append(f"attach {pid}")
        return int(self.attaches)


class FakeHelper:
    def __init__(self, argv: list[str], events: list[str], **kwargs: Any) -> None:
        self.argv, self.kwargs, self.events = argv, kwargs, events
        self.pid = 4242
        self.stdout = io.BytesIO(b"ready\r\n")

    def kill(self) -> None:
        assert self.stdout.tell(), "killed before its console was ready"
        self.events.append("kill")

    def communicate(self) -> tuple[None, None]:
        self.events.append("reaped")
        return None, None


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeKernel32]:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")

    def make(**kwargs: Any) -> FakeKernel32:
        kernel32 = FakeKernel32(**kwargs)
        monkeypatch.setattr(_console, "_kernel32", lambda: kernel32)
        return kernel32

    return make


@pytest.fixture
def helpers(monkeypatch: pytest.MonkeyPatch) -> list[FakeHelper]:
    made: list[FakeHelper] = []

    def popen(argv: list[str], **kwargs: Any) -> FakeHelper:
        kernel32 = _console._kernel32()
        made.append(FakeHelper(argv, kernel32.events, **kwargs))
        return made[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    return made


def test_elsewhere_there_is_nothing_to_do(
    helpers: list[FakeHelper], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _console.attach_hidden() is False and helpers == []


def test_a_console_already_there_is_kept(
    windows: Callable[..., FakeKernel32], helpers: list[FakeHelper]
) -> None:
    kernel32 = windows(cp=437)
    assert _console.attach_hidden() is False
    assert helpers == [] and kernel32.events == []


def test_attaches_to_the_helpers_windowless_console(
    windows: Callable[..., FakeKernel32], helpers: list[FakeHelper]
) -> None:
    kernel32 = windows()
    assert _console.attach_hidden() is True
    (helper,) = helpers
    assert helper.argv == [r"C:\Windows\system32\cmd.exe", "/d", "/q", "/c", "echo ready&pause>nul"]
    assert helper.kwargs["creationflags"] == 0x08000000  # CREATE_NO_WINDOW
    assert helper.kwargs["stdin"] == subprocess.PIPE  # kept open: it waits until killed
    # attached once its console was ready, then the helper is gone (the console isn't)
    assert kernel32.events == ["attach 4242", "kill", "reaped"]


def test_a_failed_attach_is_logged(
    windows: Callable[..., FakeKernel32],
    helpers: list[FakeHelper],
    caplog: pytest.LogCaptureFixture,
) -> None:
    kernel32 = windows(attaches=False)
    with caplog.at_level(logging.WARNING, "qsv_client.tray"):
        assert _console.attach_hidden() is False
    assert "AttachConsole failed" in caplog.text
    assert kernel32.events == ["attach 4242", "kill", "reaped"]


def test_no_helper_is_logged(
    windows: Callable[..., FakeKernel32],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    windows()

    def missing(argv: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(2, "The system cannot find the file specified", argv[0])

    monkeypatch.setattr(subprocess, "Popen", missing)
    with caplog.at_level(logging.WARNING, "qsv_client.tray"):
        assert _console.attach_hidden() is False
    assert "each qsv run may open a console window" in caplog.text


def test_the_kernel32_seam() -> None:
    if sys.platform == "win32":
        assert _console._kernel32().GetConsoleCP() != 0  # pytest runs in a console
    else:
        with pytest.raises(OSError, match="only on Windows"):
            _console._kernel32()


# Run without a console, as pythonw is: a timed-out run gets no CTRL_BREAK_EVENT until the
# tray attaches to the hidden console, and then it does, with no console window.
DRIVER = r"""
import ctypes, pathlib, sys
from qsv_client import Qsv, QsvTimeout
from qsv_client.tray import _console

fake, marker = sys.argv[1], pathlib.Path(sys.argv[2])
kernel32 = ctypes.WinDLL("kernel32")
kernel32.GetConsoleWindow.restype = ctypes.c_void_p

def stopped_politely():
    marker.unlink(missing_ok=True)
    try:
        Qsv(fake, kill_grace=3).run("sleep", str(marker), timeout=5)
    except QsvTimeout:
        pass
    return marker.exists()

print("before", bool(kernel32.GetConsoleCP()), stopped_politely())
print("attached", _console.attach_hidden())
print("after", bool(kernel32.GetConsoleCP()), kernel32.GetConsoleWindow(), stopped_politely())
"""

SLEEP_UNTIL_BREAK = """
import pathlib, signal, time
def stop(*_):
    pathlib.Path(sys.argv[-1]).write_text("break")
    sys.exit(1)
signal.signal(signal.SIGBREAK, stop)
for _ in range(600):  # short sleeps: on Windows only SIGINT wakes a sleep for its handler
    time.sleep(0.1)
"""


def test_a_console_less_tray_stops_qsv_politely(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows consoles")
    detached = 0x00000008  # DETACHED_PROCESS: no console, like pythonw
    out = subprocess.run(
        [sys.executable, "-c", DRIVER, fake_qsv(SLEEP_UNTIL_BREAK), str(tmp_path / "broke")],
        creationflags=detached,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "before False False",
        "attached True",
        "after True None True",
    ]
