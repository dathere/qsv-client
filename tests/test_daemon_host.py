"""DaemonHost: a daemon on a background thread, as the tray runs it."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from qsv_client.daemon.config import DaemonConfig
from qsv_client.daemon.host import DaemonHost
from qsv_client.daemon.server import Daemon


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="qdh"))  # short: Unix socket paths are limited
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(path))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _daemon(home: Path, fake_qsv: Callable[[str], str]) -> Daemon:
    return Daemon(DaemonConfig(binary=fake_qsv("")), home=home, tcp_port=0)


def test_stop_before_start_closes_the_loop(home: Path, fake_qsv: Callable[[str], str]) -> None:
    host = DaemonHost(_daemon(home, fake_qsv))
    host.stop()
    assert host.loop.is_closed()


def test_stop_cancels_leftover_tasks(home: Path, fake_qsv: Callable[[str], str]) -> None:
    host = DaemonHost(_daemon(home, fake_qsv)).start()
    leftover = asyncio.run_coroutine_threadsafe(asyncio.sleep(3600), host.loop)
    host.stop()
    assert leftover.cancelled()
    assert host.loop.is_closed()
    assert not (home / "run" / "local-token").exists()


def test_restart_is_a_new_daemon_and_host(home: Path, fake_qsv: Callable[[str], str]) -> None:
    first = DaemonHost(_daemon(home, fake_qsv)).start()
    token = first.daemon.local_token
    first.stop()
    second = DaemonHost(_daemon(home, fake_qsv)).start()
    try:
        assert second.daemon.local_token != token
        assert second.client().available()
    finally:
        second.stop()
