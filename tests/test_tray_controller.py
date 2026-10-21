"""The tray controller against real daemons (running a fake qsv): hosted and attached."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from daemon_helpers import DaemonThread

from qsv_client.daemon.client import DaemonClient
from qsv_client.daemon.config import DaemonConfig
from qsv_client.daemon.host import DaemonHost
from qsv_client.daemon.server import Daemon
from qsv_client.errors import QsvDaemonUnavailable, QsvNotFound
from qsv_client.tray import controller as controller_mod
from qsv_client.tray import model
from qsv_client.tray.controller import REFRESH, Controller
from qsv_client.tray.model import Mode, TrayState


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="qtc"))  # short: Unix socket paths are limited
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(path))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def make_daemon(home: Path, fake_qsv: Callable[[str], str]) -> Callable[[], Daemon]:
    binary = fake_qsv("")
    return lambda: Daemon(DaemonConfig(binary=binary), home=home, tcp_port=0)


MakeController = Callable[..., Controller]


@pytest.fixture
def make_controller(make_daemon: Callable[[], Daemon]) -> Iterator[MakeController]:
    made: list[Controller] = []

    def make(**overrides: object) -> Controller:
        settings: dict[str, object] = {
            "make_daemon": make_daemon,
            "service_installed": lambda: None,
            "stop_service": lambda: "stopped",
            "poll_interval": 0.05,
        }
        settings.update(overrides)
        c = Controller(**settings)  # type: ignore[arg-type]
        made.append(c)
        return c

    yield make
    for c in made:
        c.shutdown()


@pytest.fixture
def outside(make_daemon: Callable[[], Daemon]) -> Iterator[DaemonThread]:
    """A daemon some other process runs (a service, or `qsv-clientd serve`)."""
    thread = DaemonThread(make_daemon()).start()
    yield thread
    thread.stop()


def _do(c: Controller, action: str) -> TrayState:
    c.submit(action).result(60)
    return c.state


def _wait_for(c: Controller, mode: Mode) -> TrayState:
    deadline = time.monotonic() + 30
    while c.state.mode is not mode:
        assert time.monotonic() < deadline, c.state
        time.sleep(0.02)
    return c.state


def test_hosts_a_daemon_when_none_is_running(make_controller: MakeController, home: Path) -> None:
    c = make_controller()
    seen: list[TrayState] = []
    c.on_change = seen.append
    state = _do(c, model.START)
    assert state.mode is Mode.HOSTED and state.status is not None
    assert state.status["qsv"]["version"] == "25.0.0"
    assert [s.mode for s in seen] == [Mode.STARTING, Mode.HOSTED]
    assert DaemonClient().available()
    assert (home / "run" / "local-token").exists()

    state = _do(c, model.STOP)
    assert state == TrayState(Mode.STOPPED)
    assert seen[-2].busy == "stopping…"
    assert not (home / "run" / "local-token").exists()


def test_restart_is_a_new_daemon(make_controller: MakeController) -> None:
    c = make_controller()
    first = _do(c, model.START).status
    assert first is not None
    state = _do(c, model.RESTART)
    assert state.mode is Mode.HOSTED and state.busy is None
    assert c._host is not None and c._host.daemon.running == 0


def test_shutdown_stops_the_hosted_daemon(make_controller: MakeController, home: Path) -> None:
    c = make_controller()
    _do(c, model.START)
    c.start_polling()
    c.shutdown()
    assert not (home / "run" / "local-token").exists()
    with pytest.raises(RuntimeError):
        c.submit(REFRESH)


def test_attaches_to_a_running_daemon(
    make_controller: MakeController, outside: DaemonThread
) -> None:
    c = make_controller(service_installed=lambda: "systemd")
    state = _do(c, model.START)
    assert state.mode is Mode.ATTACHED and state.service == "systemd"
    assert state.status is not None and "pid" in state.status
    assert c._host is None
    # quitting the tray leaves someone else's daemon alone
    c.shutdown()
    assert outside.client().available()


def test_attached_daemon_going_away_is_stopped(
    make_controller: MakeController, outside: DaemonThread
) -> None:
    c = make_controller()
    _do(c, model.START)
    c.start_polling()
    outside.stop()
    assert _wait_for(c, Mode.STOPPED) == TrayState(Mode.STOPPED)


def test_a_stopped_tray_notices_a_daemon_started_elsewhere(
    make_controller: MakeController, make_daemon: Callable[[], Daemon]
) -> None:
    c = make_controller()
    assert _do(c, REFRESH) == TrayState()  # nothing running: nothing to attach to
    c.start_polling()
    later = DaemonThread(make_daemon()).start()
    try:
        assert _wait_for(c, Mode.ATTACHED).status is not None
    finally:
        later.stop()


def test_start_attaches_instead_of_hosting_a_second_daemon(
    make_controller: MakeController, make_daemon: Callable[[], Daemon]
) -> None:
    c = make_controller()
    _do(c, model.START)
    _do(c, model.STOP)
    other = DaemonThread(make_daemon()).start()
    try:
        assert _do(c, model.START).mode is Mode.ATTACHED
        assert c._host is None
    finally:
        other.stop()


def test_takeover_stops_the_service_and_hosts(
    make_controller: MakeController, outside: DaemonThread
) -> None:
    def stop_service() -> str:
        outside.stop()
        return "stopped launchd agent"

    c = make_controller(service_installed=lambda: "launchd", stop_service=stop_service)
    assert _do(c, model.START).mode is Mode.ATTACHED
    seen: list[TrayState] = []
    c.on_change = seen.append
    state = _do(c, model.TAKEOVER)
    assert state.mode is Mode.HOSTED and state.service is None
    assert seen[0].busy == "stopping the service…"


def test_takeover_gives_up_when_the_service_keeps_running(
    make_controller: MakeController, outside: DaemonThread
) -> None:
    c = make_controller(service_installed=lambda: "launchd", takeover_timeout=0.3)
    _do(c, model.START)
    c.start_polling()  # its refreshes wait behind the takeover, one at a time
    state = _do(c, model.TAKEOVER)
    assert state.mode is Mode.ERROR
    assert state.error == "the service is still running after it was stopped"
    assert state.busy is None


def test_a_failed_start_is_an_error_until_retried(
    make_controller: MakeController, make_daemon: Callable[[], Daemon]
) -> None:
    attempts: list[int] = []

    def flaky() -> Daemon:
        attempts.append(1)
        if len(attempts) == 1:
            raise QsvNotFound("qsv not found on PATH")
        return make_daemon()

    c = make_controller(make_daemon=flaky)
    state = _do(c, model.START)
    assert state.mode is Mode.ERROR and state.error == "qsv not found on PATH"
    assert _do(c, model.RETRY).mode is Mode.HOSTED


def test_an_error_without_a_message_names_its_type(make_controller: MakeController) -> None:
    def broken() -> Daemon:
        raise ValueError

    assert _do(make_controller(make_daemon=broken), model.START).error == "ValueError"


def test_a_hosted_daemon_that_stops_answering_is_an_error(
    make_controller: MakeController, home: Path
) -> None:
    c = make_controller()
    _do(c, model.START)
    assert c._host is not None
    c._host.stop()  # behind the controller's back
    state = _do(c, REFRESH)
    assert state.mode is Mode.ERROR
    assert state.error is not None and state.error.startswith("qsv-clientd stopped answering")
    # the host is kept so a retry can clean it up
    assert _do(c, model.RETRY).mode is Mode.HOSTED


def test_refresh_publishes_only_changes(make_controller: MakeController) -> None:
    c = make_controller()
    _do(c, model.START)
    seen: list[TrayState] = []
    c.on_change = seen.append
    _do(c, REFRESH)
    assert seen == []  # same status: nothing to redraw
    assert c._host is not None
    c._host.daemon.running = 1
    _do(c, REFRESH)
    assert seen[-1].status is not None and seen[-1].status["jobs"]["running"] == 1
    c._host.daemon.running = 0


def test_refresh_waits_for_busy_and_ignores_errors(make_controller: MakeController) -> None:
    c = make_controller()
    c._set(mode=Mode.HOSTED, busy="stopping…")
    assert _do(c, REFRESH).busy == "stopping…"
    c._set(mode=Mode.ERROR, busy=None, error="boom")
    assert _do(c, REFRESH).mode is Mode.ERROR


def test_no_token_file_on_windows_means_nothing_running(
    make_controller: MakeController,
) -> None:
    def no_daemon() -> DaemonClient:
        raise QsvDaemonUnavailable("qsv-clientd is not running (no token)")

    c = make_controller(attach=no_daemon)
    assert _do(c, REFRESH) == TrayState()
    assert _do(c, model.START).mode is Mode.HOSTED


def test_a_failing_listener_is_logged_not_fatal(
    make_controller: MakeController, caplog: pytest.LogCaptureFixture
) -> None:
    c = make_controller()

    def broken(state: TrayState) -> None:
        raise RuntimeError("no display")

    c.on_change = broken
    with caplog.at_level(logging.ERROR, logger="qsv_client.tray"):
        assert _do(c, model.START).mode is Mode.HOSTED
    assert "updating the tray failed" in caplog.text


def test_shutdown_logs_a_failed_stop(
    make_controller: MakeController,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = make_controller()
    _do(c, model.START)
    assert c._host is not None
    real_stop = c._host.stop

    def failing_stop() -> None:
        real_stop()
        raise TimeoutError("qsv-clientd did not stop within 40s")

    monkeypatch.setattr(c._host, "stop", failing_stop)
    with caplog.at_level(logging.ERROR, logger="qsv_client.tray"):
        c.shutdown()
    assert "stopping qsv-clientd failed" in caplog.text


class SlowHost(DaemonHost):
    """A host whose first start (or stop) times out, though the daemon goes on running."""

    start_times_out = False
    stop_times_out = False

    def start(self) -> DaemonHost:
        super().start()
        if SlowHost.start_times_out:
            SlowHost.start_times_out = False
            raise TimeoutError("qsv-clientd did not start within 60s")
        return self

    def stop(self) -> None:
        if SlowHost.stop_times_out:
            SlowHost.stop_times_out = False
            raise TimeoutError("qsv-clientd did not stop within 40s")
        super().stop()


@pytest.fixture
def slow_host(monkeypatch: pytest.MonkeyPatch) -> type[SlowHost]:
    monkeypatch.setattr(controller_mod, "DaemonHost", SlowHost)
    SlowHost.start_times_out = SlowHost.stop_times_out = False
    return SlowHost


def test_a_start_that_timed_out_is_still_stopped(
    make_controller: MakeController, slow_host: type[SlowHost], home: Path
) -> None:
    slow_host.start_times_out = True
    c = make_controller()
    state = _do(c, model.START)
    assert state.mode is Mode.ERROR and state.error == "qsv-clientd did not start within 60s"
    assert c._host is not None  # it may come up yet: keep it to stop
    assert _do(c, model.RETRY).mode is Mode.HOSTED
    c.shutdown()
    assert not (home / "run" / "local-token").exists()


def test_a_stop_that_timed_out_is_tried_again(
    make_controller: MakeController, slow_host: type[SlowHost], home: Path
) -> None:
    c = make_controller()
    _do(c, model.START)
    slow_host.stop_times_out = True
    assert _do(c, model.STOP).mode is Mode.ERROR
    assert c._host is not None and (home / "run" / "local-token").exists()
    c.shutdown()
    assert c._host is None and not (home / "run" / "local-token").exists()


def test_the_default_daemon_reads_daemon_json(home: Path, fake_qsv: Callable[[str], str]) -> None:
    (home / "daemon.json").write_text(json.dumps({"binary": fake_qsv(""), "port": 0}))
    daemon = controller_mod._load_daemon()
    assert daemon.binary == fake_qsv("") and daemon.config.port == 0
