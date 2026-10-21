"""Choosing a notification backend, and never letting one fail the tray."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import types
from typing import Any

import pytest

from qsv_client.tray import notify


def test_pystray_notifications_when_the_backend_has_them() -> None:
    shown: list[tuple[str, str]] = []
    icon = types.SimpleNamespace(
        HAS_NOTIFICATION=True, notify=lambda message, title: shown.append((title, message))
    )
    notify.choose(icon)("Pairing request", "ABC-123: 'Sales'")
    assert shown == [("Pairing request", "ABC-123: 'Sales'")]


def test_a_failing_backend_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    def fail(message: str, title: str) -> None:
        raise subprocess.CalledProcessError(1, ["osascript"])

    icon = types.SimpleNamespace(HAS_NOTIFICATION=True, notify=fail)
    with caplog.at_level(logging.WARNING, logger="qsv_client.tray"):
        notify.choose(icon)("t", "m")
    assert "a notification through pystray failed" in caplog.text


def test_notify_send_on_linux_without_pystray_notifications(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs))
        if len(calls) > 1:
            raise FileNotFoundError(args[0])  # uninstalled since
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    send = notify.choose(types.SimpleNamespace(HAS_NOTIFICATION=False))
    send("Pairing request", 'a "quoted" title')
    assert calls[0][0] == [
        "/usr/bin/notify-send",
        "--app-name=qsv-clientd",
        "Pairing request",
        'a "quoted" title',
    ]
    assert calls[0][1]["check"] is True and calls[0][1]["timeout"] == 10
    with caplog.at_level(logging.WARNING, logger="qsv_client.tray"):
        send("t", "m")
    assert "a notification through notify-send failed" in caplog.text


@pytest.mark.parametrize(("platform", "which"), [("linux", None), ("win32", "/x/notify-send")])
def test_no_backend_means_no_notifications(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    platform: str,
    which: str | None,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", lambda name: which)
    with caplog.at_level(logging.INFO, logger="qsv_client.tray"):
        send = notify.choose(object())
    assert send is notify.nothing
    send("t", "m")
    assert "no desktop notifications here" in caplog.text


def test_windows_balloons_fit_their_buffers(monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[tuple[str, str]] = []
    icon = types.SimpleNamespace(
        HAS_NOTIFICATION=True, notify=lambda message, title: shown.append((title, message))
    )
    monkeypatch.setattr(sys, "platform", "win32")
    message = "ABC-123: '" + "😀" * 200 + "'. Approve or deny it from the qsv-clientd menu."
    notify.choose(icon)("T" * 100, message)
    ((title, sent),) = shown
    assert title == "T" * 62 + "…"
    assert sent.startswith("ABC-123: '") and sent.endswith("…")  # the code survives
    assert len(sent.encode("utf-16-le")) // 2 <= 255
    monkeypatch.setattr(sys, "platform", "darwin")
    assert notify.fit_windows("x" * 300, 255) == "x" * 300
