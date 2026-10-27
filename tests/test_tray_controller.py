"""The tray controller against real daemons (running a fake qsv): hosted and attached."""

from __future__ import annotations

import hashlib
import http.server
import json
import logging
import shutil
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import DaemonThread

from qsv_client.daemon.client import DaemonClient
from qsv_client.daemon.config import DaemonConfig, config_template
from qsv_client.daemon.host import DaemonHost
from qsv_client.daemon.server import Daemon
from qsv_client.errors import QsvDaemonError, QsvDaemonUnavailable, QsvNotFound
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


def _qsv_reporting(version: str) -> str:
    """A fake qsv body whose --capabilities reports ``version``."""
    caps = json.dumps({"binary": "qsv", "version": version, "error_formats": ["text", "json"]})
    return f"""
        if sys.argv[1:] == ["--capabilities"]:
            print({caps!r})
            sys.exit(0)
    """


@pytest.fixture
def make_daemon(home: Path, fake_qsv: Callable[[str], str]) -> Callable[[], Daemon]:
    # the first qsv whose schematics go live: older ones add a notification
    binary = fake_qsv(_qsv_reporting("27.0.0"))
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
            # never the real login items or files of whoever runs the tests
            "autostart_enabled": lambda: None,
            "open_path": lambda path: None,
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
    assert state.status["qsv"]["version"] == "27.0.0"
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


def test_the_default_daemon_reads_daemon_toml(home: Path, fake_qsv: Callable[[str], str]) -> None:
    (home / "daemon.toml").write_text(f"binary = {json.dumps(fake_qsv(''))}\nport = 0\n")
    daemon = controller_mod.load_daemon()
    assert daemon.binary == fake_qsv("") and daemon.config.port == 0
    (home / "daemon.toml").write_text('binary = "qsv"\nport = 0\n')  # bare name
    assert controller_mod.load_daemon(fake_qsv("")).binary == fake_qsv("")  # --binary wins


# -- pairing requests ---------------------------------------------------------------------------


class Notes(list[tuple[str, str]]):
    def __call__(self, title: str, message: str) -> None:
        self.append((title, message))


def test_a_qsv_older_than_27_is_noted_once(
    make_controller: MakeController, home: Path, fake_qsv: Callable[[str], str]
) -> None:
    binary = fake_qsv(_qsv_reporting("26.0.0"))
    c = make_controller(
        make_daemon=lambda: Daemon(DaemonConfig(binary=binary), home=home, tcp_port=0)
    )
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    _do(c, model.RESTART)
    assert notes == [
        (
            "Data Schematics won't go live",
            "qsv 26.0.0 writes Data Schematics that can't connect to qsv-clientd. "
            "Live schematics need qsv 27.0.1 or later.",
        )
    ]


@pytest.fixture
def data(tmp_path: Path) -> Path:
    path = tmp_path / "sales.csv"
    path.write_text("name,n\nalpha,1\n")
    return path


def _local(d: DaemonThread, method: str, path: str, body: object = None) -> Any:
    reply = d.http(method, path, body, origin=None, token=d.daemon.local_token)
    assert reply.status == 200, reply.body
    return reply.json()


def _ask_to_pair(d: DaemonThread, digest: str) -> dict[str, Any]:
    reply = d.http("POST", "/v1/pair", {"digest": digest})  # as a file:// page would
    assert reply.status == 200, reply.body
    request: dict[str, Any] = reply.json()
    return request


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_requests_are_listed_announced_once_and_approved(
    make_controller: MakeController, outside: DaemonThread, data: Path
) -> None:
    _local(outside, "POST", "/v1/datasets", {"path": str(data), "title": "Sales"})
    c = make_controller()
    notes = Notes()
    c.notify = notes
    assert _do(c, model.START).pairings == ()
    page = _ask_to_pair(outside, _sha(data))
    state = _do(c, REFRESH)
    (request,) = state.pairings
    assert request.text.code == page["code"] and request.text.title == "Sales"
    # 300s less however long the fetch took: none at all with Windows' 15.6ms clock ticks
    assert request.expires in ("expires in 5m", "expires in 4m")
    assert notes == [
        (
            "Pairing request",
            "'Sales' wants to pair. To approve, pick the code the page shows in the "
            "qsv-clientd menu.",
        )
    ]
    assert page["code"] not in repr(notes)  # recognized from the page, never told
    _do(c, REFRESH)
    assert len(notes) == 1  # once per request
    c.submit(model.APPROVE, page["code"]).result(60)
    assert c.state.pairings == ()
    poll = outside.http("GET", f"/v1/pair/{page['request_id']}").json()
    assert poll["status"] == "approved"


def test_unregistered_and_non_file_requests_say_so(
    make_controller: MakeController, outside: DaemonThread, data: Path
) -> None:
    _local(outside, "POST", "/v1/datasets", {"path": str(data), "title": "Sales"})
    c = make_controller()
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    stranger = _ask_to_pair(outside, "0" * 64)
    local = _local(outside, "POST", "/v1/pair", {"digest": _sha(data)})  # origin "local"
    _do(c, REFRESH)
    assert sorted(m for _, m in notes) == sorted(
        [
            "A schematic that matches no registered dataset wants to pair. Deny it from the "
            "qsv-clientd menu, or register the dataset first.",
            "'Sales' wants to pair, from local (not a local file). To approve, pick the code "
            "the page shows in the qsv-clientd menu.",
        ]
    )
    c.submit(model.DENY, stranger["code"]).result(60)
    assert [p.text.code for p in c.state.pairings] == [local["code"]]
    poll = outside.http("GET", f"/v1/pair/{stranger['request_id']}").json()
    assert poll["status"] == "denied"


def test_an_expired_or_unregistered_code_is_a_notification_not_an_error(
    make_controller: MakeController, outside: DaemonThread
) -> None:
    c = make_controller()
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    c.submit(model.APPROVE, "ZZZ-999").result(60)
    stranger = _ask_to_pair(outside, "0" * 64)
    c.submit(model.APPROVE, stranger["code"]).result(60)  # the menu never offers this
    assert c.state.mode is Mode.ATTACHED
    (expired, unregistered) = [n for n in notes if n[0].startswith("Couldn't")]
    assert expired[0] == "Couldn't approve the pairing request" and "expired" in expired[1]
    assert unregistered[0] == "Couldn't approve the pairing request"
    assert "register it first" in unregistered[1]


def test_deciding_without_a_daemon(make_controller: MakeController, outside: DaemonThread) -> None:
    c = make_controller()
    c.submit(model.DENY, "ABC-123").result(60)  # never started: nothing to tell
    assert c.state == TrayState()
    _do(c, model.START)
    outside.stop()
    state = c.submit(model.APPROVE, "ABC-123").result(60)
    assert state is None and c.state.mode is Mode.ERROR  # the daemon went away


def test_a_hosted_daemon_lists_requests_too(
    make_controller: MakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller_mod, "DaemonHost", DaemonThread)
    c = make_controller()
    _do(c, model.START)
    assert isinstance(c._host, DaemonThread)
    page = _ask_to_pair(c._host, "0" * 64)
    assert [p.text.code for p in _do(c, REFRESH).pairings] == [page["code"]]
    # and the attention dot goes with the daemon
    assert _do(c, model.STOP).pairings == ()


def test_picking_a_decoy_denies_the_request(
    make_controller: MakeController, outside: DaemonThread, data: Path
) -> None:
    _local(outside, "POST", "/v1/datasets", {"path": str(data), "title": "Sales"})
    c = make_controller()
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    page = _ask_to_pair(outside, _sha(data))
    _do(c, REFRESH)
    c.submit(model.WRONG_CODE, page["code"]).result(60)
    assert c.state.pairings == ()
    assert outside.http("GET", f"/v1/pair/{page['request_id']}").json()["status"] == "denied"
    title, message = notes[-1]
    assert title == "Pairing denied" and message.startswith("The code you picked isn't")


def test_a_decoy_picked_for_an_expired_request_says_only_that(
    make_controller: MakeController, outside: DaemonThread
) -> None:
    c = make_controller()
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    c.submit(model.WRONG_CODE, "ZZZ-999").result(60)
    assert [title for title, _ in notes] == ["Couldn't deny the pairing request"]


# -- datasets -----------------------------------------------------------------------------------

# enough qsv for render and fetch: viz writes --output; slice gives a file's header row
DATASET_QSV = """
cmd = sys.argv[1]
if cmd == "viz":
    open(sys.argv[sys.argv.index("--output") + 1], "w").write("<html>re-rendered</html>")
elif cmd == "slice":
    print(open(sys.argv[-1]).readline().rstrip("\\n"))
else:
    print("1")
"""


class Web:
    """A loopback HTTP server: GET /data.csv answers ``body``."""

    def __init__(self) -> None:
        self.body = b"name,n\nalpha,2\n"
        web = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(web.body)))
                self.end_headers()
                self.wfile.write(web.body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/data.csv"


@pytest.fixture
def web() -> Iterator[Web]:
    server = Web()
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


@pytest.fixture
def registered(
    outside: DaemonThread, fake_qsv: Callable[[str], str], data: Path, web: Web
) -> tuple[DaemonThread, str, Path]:
    """The outside daemon, with a dataset registered (schematic, source URL): its id."""
    fake_qsv(DATASET_QSV)  # the same path the daemon runs: now it can render and fetch
    schematic = data.with_suffix(".html")
    schematic.write_text("<html>first</html>")
    body = {
        "path": str(data),
        "schematic_path": str(schematic),
        "source_url": web.url,
        "title": "Sales",
    }
    ds = _local(outside, "POST", "/v1/datasets", body)
    return outside, ds["id"], schematic


def _until(c: Controller, done: Callable[[TrayState], bool]) -> TrayState:
    deadline = time.monotonic() + 30
    while not done(c.state):
        assert time.monotonic() < deadline, c.state
        time.sleep(0.02)
    return c.state


def test_datasets_are_listed_and_re_rendered(
    make_controller: MakeController, registered: tuple[DaemonThread, str, Path]
) -> None:
    _, dataset_id, schematic = registered
    c = make_controller()
    notes = Notes()
    c.notify = notes
    (ds,) = _do(c, model.START).datasets
    assert (ds.id, ds.title, ds.schematic_path) == (dataset_id, "Sales", str(schematic))
    seen: list[TrayState] = []
    c.on_change = seen.append
    c.submit(model.RENDER, dataset_id).result(60)
    _until(c, lambda s: not s.jobs and bool(notes))
    assert [j.text for s in seen for j in s.jobs][:1] == ["rendering 'Sales'…"]
    assert notes == [("Re-rendered 'Sales'", "Its schematic was rewritten (24 bytes).")]
    assert schematic.read_text() == "<html>re-rendered</html>"


def test_fetching_and_accepting_missing_columns(
    make_controller: MakeController, registered: tuple[DaemonThread, str, Path], web: Web
) -> None:
    _, dataset_id, _ = registered
    c = make_controller()
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    c.submit(model.FETCH, dataset_id).result(60)
    _until(c, lambda s: not s.jobs and len(notes) == 1)
    assert notes[-1] == ("Fetched 'Sales'", "The latest data was downloaded and is now in use.")
    (ds,) = _until(c, lambda s: s.datasets[0].fetched).datasets
    assert ds.path.endswith("sales.csv")  # still named by the user's file

    web.body = b"other\nx\n"  # the dataset's columns are gone
    c.submit(model.FETCH, dataset_id).result(60)
    state = _until(c, lambda s: not s.jobs and len(notes) == 2)
    assert notes[-1][0] == "Couldn't fetch 'Sales'"
    assert "accepting missing columns" in notes[-1][1]
    assert state.force_fetch == {dataset_id}

    c.submit(model.FETCH_FORCE, dataset_id).result(60)
    state = _until(c, lambda s: not s.jobs and len(notes) == 3)
    assert notes[-1][0] == "Fetched 'Sales'" and state.force_fetch == frozenset()


def test_opening_a_schematic(
    make_controller: MakeController,
    registered: tuple[DaemonThread, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dataset_id, schematic = registered
    opened: list[str] = []
    answers = [True, False]

    def open_url(url: str) -> bool:
        opened.append(url)
        return answers.pop(0)

    monkeypatch.setattr(webbrowser, "open", open_url)
    c = make_controller()
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    c.submit(model.OPEN_SCHEMATIC, dataset_id).result(60)
    assert opened == [schematic.as_uri()] and notes == []
    c.submit(model.OPEN_SCHEMATIC, dataset_id).result(60)  # no browser could
    assert notes == [("Couldn't open 'Sales'", f"No browser could open {schematic}.")]


def test_forgetting_a_dataset_and_revoking_tokens(
    make_controller: MakeController, registered: tuple[DaemonThread, str, Path], data: Path
) -> None:
    outside, dataset_id, _ = registered
    outside.pair(_sha(data))
    c = make_controller()
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    c.submit(model.REVOKE_ALL).result(60)
    assert notes[-1] == ("Tokens revoked", "1 token revoked; open pages must pair again.")
    outside.pair(_sha(data))
    stale = c.state.datasets
    c.submit(model.FORGET, dataset_id).result(60)
    assert notes[-1] == ("Forgot 'Sales'", "It is no longer registered; 1 token revoked.")
    assert c.state.datasets == ()
    c._set(datasets=stale)  # a menu drawn before the forget
    c.submit(model.FORGET, dataset_id).result(60)
    assert notes[-1][0] == "Couldn't forget 'Sales'"
    assert c.state.mode is Mode.ATTACHED
    outside.stop()  # not a 404: the daemon is gone, which the tray shows as an error
    c._set(datasets=stale)
    c.submit(model.FORGET, dataset_id).result(60)
    after = c.state
    assert after.mode is Mode.ERROR


def test_dataset_actions_without_a_dataset_or_daemon(make_controller: MakeController) -> None:
    c = make_controller()
    (ds,) = model.dataset_views([{"id": "ds1", "path": "/d.csv", "schematic_path": "/d.html"}])
    for action in (model.RENDER, model.FORGET, model.OPEN_SCHEMATIC, model.REVOKE_ALL):
        c.submit(action, None if action == model.REVOKE_ALL else "missing").result(60)
    c._set(mode=Mode.HOSTED, datasets=(ds,))  # listed, but there is no daemon to ask
    for action in (model.RENDER, model.FORGET, model.REVOKE_ALL):
        c.submit(action, None if action == model.REVOKE_ALL else "ds1").result(60)
    assert c.state.jobs == () and c.state.mode is Mode.HOSTED


class Stub:
    """A client whose requests fail as given."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def request(self, *args: object, **kwargs: object) -> Any:
        raise self.exc


def test_a_job_reports_what_went_wrong_unless_the_tray_is_quitting(
    make_controller: MakeController, caplog: pytest.LogCaptureFixture
) -> None:
    c = make_controller()
    notes = Notes()
    c.notify = notes
    (ds,) = model.dataset_views([{"id": "ds1", "path": "/d.csv", "title": "D"}])
    job = model.Job("ds1", "rendering 'D'…")
    c._set(jobs=(job,))
    with caplog.at_level(logging.ERROR, logger="qsv_client.tray"):
        c._job(model.RENDER, ds, Stub(RuntimeError("boom")), job)  # type: ignore[arg-type]
    assert notes == [("Couldn't render 'D'", "See the tray's log.")]
    assert "render of ds1 failed" in caplog.text and c.state.jobs == ()
    refused = QsvDaemonError("no schematic or viz arguments registered", status=409)
    c._job(model.RENDER, ds, Stub(refused), job)  # type: ignore[arg-type]
    assert notes[-1] == ("Couldn't render 'D'", "no schematic or viz arguments registered")
    c._stopping.set()  # Quit: the hosted daemon is stopped under the job
    c._set(jobs=(job,))
    gone = QsvDaemonUnavailable("qsv-clientd is not running")
    c._job(model.FETCH, ds, Stub(gone), job)  # type: ignore[arg-type]
    c._job(model.FETCH, ds, Stub(RuntimeError("late")), job)  # type: ignore[arg-type]
    assert len(notes) == 2 and c.state.jobs == ()


def test_one_job_per_dataset_and_forgotten_datasets_lose_force(
    make_controller: MakeController, registered: tuple[DaemonThread, str, Path]
) -> None:
    _, dataset_id, _ = registered
    c = make_controller()
    _do(c, model.START)
    job = model.Job(dataset_id, "fetching 'Sales'…")
    c._set(jobs=(job,), force_fetch=frozenset({dataset_id, "gone"}))
    c.submit(model.FETCH, dataset_id).result(60)
    assert c.state.jobs == (job,)  # the running one holds it
    c._set(datasets=())  # forces a refresh to publish the list again
    assert _do(c, REFRESH).force_fetch == {dataset_id}


def test_the_tray_logs_no_pairing_codes(
    make_controller: MakeController,
    outside: DaemonThread,
    data: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    _local(outside, "POST", "/v1/datasets", {"path": str(data), "title": "Sales"})
    c = make_controller()
    _do(c, model.START)
    pages = [_ask_to_pair(outside, _sha(data)) for _ in range(3)]
    _do(c, REFRESH)
    c.submit(model.APPROVE, pages[0]["code"]).result(60)
    c.submit(model.DENY, pages[1]["code"]).result(60)
    c.submit(model.WRONG_CODE, pages[2]["code"]).result(60)
    assert "tray action: approve\n" in caplog.text + "\n"
    for page in pages:
        assert page["code"] not in caplog.text


def test_concurrent_changes_reach_the_gui_in_order(make_controller: MakeController) -> None:
    c = make_controller()
    job = model.Job("ds1", "rendering 'D'…")
    seen: list[TrayState] = []
    calls: list[TrayState] = []
    first_drawing, release = threading.Event(), threading.Event()

    def on_change(state: TrayState) -> None:
        calls.append(state)
        if len(calls) == 1:  # the first redraw is slow
            first_drawing.set()
            release.wait(10)
        seen.append(state)

    c.on_change = on_change
    adder = threading.Thread(target=c._update, args=(lambda s: replace(s, jobs=(job,)),))
    adder.start()
    assert first_drawing.wait(10)
    remover = threading.Thread(target=c._update, args=(lambda s: replace(s, jobs=()),))
    remover.start()
    time.sleep(0.1)  # the job is done, while the GUI still draws it as running
    release.set()
    adder.join(10)
    remover.join(10)
    assert seen[-1].jobs == ()  # the GUI ends showing the job finished


# -- files and login ----------------------------------------------------------------------------


def _opener(c: Controller) -> list[Path]:
    opened: list[Path] = []
    c._open_path = opened.append
    return opened


@pytest.mark.parametrize(
    ("fields", "hint"),
    [
        ({"mode": Mode.HOSTED}, "Restart, in the menu, applies your changes"),
        ({"mode": Mode.ATTACHED, "service": "launchd"}, "the qsv-clientd launchd service"),
        ({"mode": Mode.ATTACHED}, "next starts: restart it"),
        ({"mode": Mode.ERROR}, "Changes apply when qsv-clientd next starts."),
    ],
)
def test_open_config_creates_it_and_says_how_changes_apply(
    make_controller: MakeController, home: Path, fields: dict[str, Any], hint: str
) -> None:
    c = make_controller()
    opened = _opener(c)
    notes = Notes()
    c.notify = notes
    c._set(**fields)
    c.submit(model.OPEN_CONFIG).result(60)
    assert opened == [home / "daemon.toml"]
    assert (home / "daemon.toml").read_text() == config_template()
    assert DaemonConfig.load() == DaemonConfig()  # all commented out: nothing changes
    ((title, message),) = notes
    assert title == "Editing daemon.toml" and hint in message


def test_open_config_keeps_an_existing_file(make_controller: MakeController, home: Path) -> None:
    (home / "daemon.toml").write_text("port = 0\n")
    c = make_controller()
    _opener(c)
    c.submit(model.OPEN_CONFIG).result(60)
    assert (home / "daemon.toml").read_text() == "port = 0\n"


def test_a_file_that_wont_open_is_a_notification(
    make_controller: MakeController, home: Path
) -> None:
    c = make_controller()
    notes = Notes()
    c.notify = notes

    def no_opener(path: Path) -> None:
        raise FileNotFoundError(2, "No such file or directory: 'xdg-open'")

    c._open_path = no_opener
    c.submit(model.OPEN_CONFIG).result(60)
    assert notes == [
        ("Couldn't open daemon.toml", "[Errno 2] No such file or directory: 'xdg-open'")
    ]
    assert c.state.mode is Mode.STOPPED


def test_open_log_opens_the_right_log(
    make_controller: MakeController, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = make_controller()
    opened = _opener(c)
    notes = Notes()
    c.notify = notes
    c._set(mode=Mode.HOSTED)
    c.submit(model.OPEN_LOG).result(60)
    assert notes == [("No log yet", f"Nothing has been logged to {home / 'qsv-clientd.log'} yet.")]
    (home / "qsv-clientd.log").write_text("x\n")
    c.submit(model.OPEN_LOG).result(60)
    assert opened == [home / "qsv-clientd.log"]
    user = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: user)
    service_log = user / "Library" / "Logs" / "qsv-clientd.log"
    service_log.parent.mkdir(parents=True)
    service_log.write_text("x\n")
    c._set(mode=Mode.ATTACHED, service="launchd")
    c.submit(model.OPEN_LOG).result(60)
    assert opened[-1] == service_log


def test_start_at_login_toggles_and_reads_back(make_controller: MakeController) -> None:
    enabled = [False]

    def enable() -> str:
        enabled[0] = True
        return "enabled"

    def disable() -> str:
        enabled[0] = False
        return "disabled"

    c = make_controller(
        autostart_enabled=lambda: enabled[0], enable_autostart=enable, disable_autostart=disable
    )
    assert _do(c, model.START).autostart is False  # read at start
    assert _do(c, model.AUTOSTART).autostart is True
    assert _do(c, model.AUTOSTART).autostart is False


def test_start_at_login_failures_are_notifications(
    make_controller: MakeController, caplog: pytest.LogCaptureFixture
) -> None:
    def no_qsv() -> str:
        raise QsvNotFound("qsv not found on PATH")

    def unreadable() -> bool:
        raise PermissionError(13, "Access is denied")

    c = make_controller(autostart_enabled=lambda: False, enable_autostart=no_qsv)
    notes = Notes()
    c.notify = notes
    _do(c, model.START)
    state = _do(c, model.AUTOSTART)
    assert notes == [("Couldn't change Start at login", "qsv not found on PATH")]
    assert state.autostart is False and state.mode is Mode.HOSTED
    c._autostart_enabled = unreadable
    with caplog.at_level(logging.WARNING, logger="qsv_client.tray"):
        assert _do(c, model.RETRY).autostart is None  # unknown: the item goes away
    assert "couldn't tell whether the tray starts at login" in caplog.text


def test_an_unwritable_config_directory_is_a_notification(
    make_controller: MakeController, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read_only(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "write_text", read_only)
    c = make_controller()
    opened = _opener(c)
    notes = Notes()
    c.notify = notes
    c._set(mode=Mode.HOSTED)
    c.submit(model.OPEN_CONFIG).result(60)
    assert opened == [] and c.state.mode is Mode.HOSTED
    assert notes[0][0] == "Couldn't create daemon.toml" and "Permission denied" in notes[0][1]
