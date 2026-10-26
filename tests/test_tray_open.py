"""Opening a file in the user's app, on each platform (nothing is really opened)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from qsv_client.tray import _open


class FakeProc:
    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv, self.kwargs, self.waited = argv, kwargs, False
        FakeProc.made.append(self)

    def wait(self) -> int:
        self.waited = True
        return 0

    made: ClassVar[list[FakeProc]] = []


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> list[FakeProc]:
    FakeProc.made = []
    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    return FakeProc.made


def _reaped(proc: FakeProc) -> bool:
    deadline = time.monotonic() + 5
    while not proc.waited and time.monotonic() < deadline:
        time.sleep(0.01)
    return proc.waited


@pytest.mark.parametrize(
    ("platform", "argv", "new_session"),
    [("darwin", ["open", "-t"], True), ("linux", ["xdg-open"], True)],
)
def test_posix_openers(
    popen: list[FakeProc],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    argv: list[str],
    new_session: bool,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    _open.open_path(tmp_path / "daemon.toml")
    (proc,) = popen
    assert proc.argv == [*argv, str(tmp_path / "daemon.toml")]
    assert proc.kwargs["start_new_session"] is new_session
    assert proc.kwargs["stdout"] == subprocess.DEVNULL
    assert _reaped(proc)  # no zombie left behind


def test_windows_opens_with_the_associated_app(
    popen: list[FakeProc], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[Path] = []
    monkeypatch.setattr(os, "startfile", started.append, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    _open.open_path(tmp_path / "qsv-clientd.log")
    assert started == [tmp_path / "qsv-clientd.log"] and popen == []


def test_windows_falls_back_to_notepad(
    popen: list[FakeProc], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_association(path: Path) -> None:
        raise OSError(1155, "No application is associated with the specified file")

    monkeypatch.setattr(os, "startfile", no_association, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    _open.open_path(tmp_path / "daemon.toml")
    (proc,) = popen
    assert proc.argv == ["notepad.exe", str(tmp_path / "daemon.toml")]
    assert proc.kwargs["start_new_session"] is False


def test_a_missing_opener_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(argv: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "Popen", missing)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(OSError):
        _open.open_path(tmp_path / "daemon.toml")
