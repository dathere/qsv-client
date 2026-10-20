"""DaemonHost: a daemon on a background thread, as the tray runs it."""

from __future__ import annotations

import asyncio
import errno
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from qsv_client.daemon import host as host_mod
from qsv_client.daemon import server
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


def test_stop_right_after_start_stops(home: Path, fake_qsv: Callable[[str], str]) -> None:
    for _ in range(5):
        host = DaemonHost(_daemon(home, fake_qsv)).start()
        host.stop()
        assert not host._thread.is_alive()
        assert not (home / "run" / "local-token").exists()


def test_a_start_that_fails_after_binding_lets_go_of_its_listeners(
    home: Path, fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(path: Path, data: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(server, "write_private_file", disk_full)
    daemon = _daemon(home, fake_qsv)
    host = DaemonHost(daemon)
    with pytest.raises(OSError, match="No space"):
        host.start()
    host.stop()  # nothing left to close
    assert daemon._servers == []
    if sys.platform != "win32":
        assert not daemon.socket_path.exists()
    monkeypatch.undo()
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(home))
    retry = DaemonHost(_daemon(home, fake_qsv)).start()
    retry.stop()


def test_start_times_out(
    home: Path, fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = DaemonHost(_daemon(home, fake_qsv))
    monkeypatch.setattr(host._ready, "wait", lambda timeout: False)
    with pytest.raises(TimeoutError, match="did not start"):
        host.start()
    monkeypatch.undo()
    host._ready.wait(60)
    host.stop()


def test_stop_reports_a_failed_close(
    home: Path, fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = DaemonHost(_daemon(home, fake_qsv)).start()
    real_close = host.daemon.close

    async def failing_close() -> None:
        await real_close()
        raise OSError("cleanup failed")

    monkeypatch.setattr(host.daemon, "close", failing_close)
    with pytest.raises(OSError, match="cleanup failed"):
        host.stop()
    assert not host._thread.is_alive() and host.loop.is_closed()


def test_stop_times_out_on_a_stuck_close(
    home: Path, fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = DaemonHost(_daemon(home, fake_qsv)).start()
    real_close = host.daemon.close

    async def slow_close() -> None:
        await asyncio.sleep(1.5)
        await real_close()

    monkeypatch.setattr(host.daemon, "close", slow_close)
    monkeypatch.setattr(host_mod, "_STOP_TIMEOUT", 0.0)
    host.daemon.config.kill_grace = 0.4  # waited twice: before and after SIGKILL
    with pytest.raises(TimeoutError, match=r"did not stop within 1s"):
        host.stop()
    host._thread.join(30)  # the loop still stops once close() is done
    assert host.loop.is_closed()
