"""qsv-clientd server edges: malformed requests, refusals per tier, and lifecycle corner cases.

Every refusal a browser can receive is checked for its status, its error code, and that no
local path leaks into it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import DaemonThread, Reply

from qsv_client.daemon import DaemonConfig, server
from qsv_client.daemon.pairing import MAX_REQUESTS_PER_MINUTE
from qsv_client.daemon.server import Daemon

StartDaemon = Callable[..., DaemonThread]


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # short path: macOS limits Unix socket paths to 104 bytes, too short for pytest's tmp_path
    path = Path(tempfile.mkdtemp(prefix="qcd"))
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(path))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def start_daemon(home: Path) -> Iterator[StartDaemon]:
    started: list[DaemonThread] = []

    def start(binary: str, **settings: object) -> DaemonThread:
        config = DaemonConfig(binary=binary, **settings)  # type: ignore[arg-type]
        socket_path = home / f"s{len(started)}.sock" if started else None
        daemon = Daemon(config, home=home, tcp_port=0, socket_path=socket_path)
        thread = DaemonThread(daemon).start()
        started.append(thread)
        return thread

    yield start
    for thread in started:
        thread.stop()


@pytest.fixture
def data(tmp_path: Path) -> Path:
    path = tmp_path / "data.csv"
    path.write_text("name,n\nalpha,1\nbeta,2\n")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _local(d: DaemonThread, method: str, path: str, body: Any = None) -> Reply:
    return d.http(method, path, body, origin=None, token=d.daemon.local_token)


def _register(d: DaemonThread, path: Path, **extra: object) -> str:
    reply = _local(d, "POST", "/v1/datasets", {"path": str(path), **extra})
    assert reply.status == 200, reply.body
    dataset_id: str = reply.json()["id"]
    return dataset_id


def _code(reply: Reply) -> str:
    code: str = reply.json()["daemon_error"]["code"]
    return code


def _assert_refused(reply: Reply, status: int, code: str, *private: Path) -> None:
    """A refusal with this status and code, mentioning none of ``private``."""
    assert reply.status == status, reply.body
    assert _code(reply) == code, reply.body
    body = reply.body.decode()
    for path in private:
        for p in (str(path), str(path.resolve())):
            # JSON escapes the backslashes in a Windows path
            assert p not in body, reply.body
            assert json.dumps(p)[1:-1] not in body, reply.body


def _logging_qsv(fake_qsv: Callable[[str], str], log: Path, body: str = "") -> str:
    """A fake qsv that appends each run's argv (not the capabilities probe) to ``log``."""
    return fake_qsv(
        f"import json\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n" + body
    )


# Enough of qsv for the dataset actions: viz writes --output, count counts, frequency
# finds nothing (empty CSV output), everything else prints a small table.
GENERIC = """
import os
cmd = sys.argv[1]
if cmd == "viz":
    out = sys.argv[sys.argv.index("--output") + 1]
    open(out, "w").write("<html>" + "x" * 100 + "</html>")
elif cmd == "count":
    print(2)
elif cmd == "frequency":
    pass
else:
    sys.stdout.write("name,n\\nalpha,1\\n")
"""


# -- local tier: malformed requests ---------------------------------------------------------


def test_malformed_local_runs_are_refused_before_qsv_runs(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    log = tmp_path / "runs.log"
    d = start_daemon(_logging_qsv(fake_qsv, log))
    bad: list[dict[str, Any]] = [
        {"command": 1},
        {"args": []},
        {"command": "count", "args": "data.csv"},
        {"command": "count", "args": [True]},
        {"command": "count", "args": [None]},
        {"command": "count", "cwd": "relative/dir"},
        {"command": "count", "cwd": 5},
        {"command": "count", "env": ["A=1"]},
        {"command": "count", "env": {"A": 1}},
        {"command": "count", "stdin_b64": "not base64!"},
        {"command": "count", "stdout_path": 5},
        {"command": "count", "timeout": 0},
        {"command": "count", "timeout": -1},
        {"command": "count", "timeout": True},
        {"command": "count", "timeout": "5"},
    ]
    for body in bad:
        reply = _local(d, "POST", "/v1/run", body)
        assert reply.status == 422, (body, reply.body)
        assert _code(reply) == "bad_request", body
    assert not log.exists(), "qsv ran for a malformed request"
    # numbers in args are stringified, and a valid timeout below the cap is accepted
    ok = _local(d, "POST", "/v1/run", {"command": "count", "args": [3, 1.5], "timeout": 30})
    assert ok.status == 200, ok.body
    assert json.loads(log.read_text().splitlines()[-1]) == ["count", "3", "1.5"]


def test_malformed_registrations_are_refused(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    d = start_daemon(fake_qsv(GENERIC))
    missing = tmp_path / "missing.csv"
    _assert_refused(_local(d, "POST", "/v1/datasets", {"path": str(missing)}), 422, "not_found")
    for viz_args in (["-o", "x.html"], ["--output=x.html"], ["-ox.html"]):
        reply = _local(d, "POST", "/v1/datasets", {"path": str(data), "viz_args": viz_args})
        _assert_refused(reply, 422, "not_allowed")
    reply = _local(d, "POST", "/v1/datasets", {"path": str(data), "viz_args": "--title x"})
    _assert_refused(reply, 422, "bad_request")
    reply = _local(d, "POST", "/v1/datasets", {"path": str(data), "schematic_path": "rel.html"})
    _assert_refused(reply, 422, "bad_request")
    assert _local(d, "GET", "/v1/datasets").json() == []


# -- local tier: routes -----------------------------------------------------------------------


def test_local_status_listing_and_unknown_routes(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    binary = fake_qsv(GENERIC)
    d = start_daemon(binary, max_jobs=3)
    ds_id = _register(d, data, title="People")

    status = _local(d, "GET", "/v1/status").json()
    assert status["service"] == "qsv-clientd" and status["api"] == 1
    assert status["pid"] == os.getpid()
    assert status["binary"] == d.daemon.binary
    assert status["qsv"]["version"] == "25.0.0"
    assert status["jobs"] == {"max": 3, "running": 0}
    assert status["datasets"] == 1 and status["pending_pairings"] == 0
    assert status["port"] == d.port
    if sys.platform != "win32":
        assert status["socket"] == str(d.daemon.socket_path)

    # the local user sees full records, paths included
    listing = _local(d, "GET", "/v1/datasets").json()
    assert [(ds["id"], ds["title"]) for ds in listing] == [(ds_id, "People")]
    assert listing[0]["path"] == str(data.resolve())
    assert _local(d, "GET", f"/v1/datasets/{ds_id}").json()["path"] == str(data.resolve())

    _assert_refused(_local(d, "GET", "/v1/datasets/nope"), 404, "not_found")
    _assert_refused(_local(d, "DELETE", "/v1/datasets/nope"), 404, "not_found")
    _assert_refused(_local(d, "GET", "/v1/nope"), 404, "not_found")
    _assert_refused(_local(d, "PUT", "/v1/status"), 404, "not_found")
    _assert_refused(_local(d, "GET", "/v2/hello"), 404, "not_found")
    _assert_refused(_local(d, "GET", "/"), 404, "not_found")
    # a page learns nothing about local routes, real or not: the tier check comes first
    _assert_refused(d.http("GET", "/v1/nope"), 401, "unauthorized")
    _assert_refused(d.http("GET", "/v1/status"), 401, "unauthorized")
    _assert_refused(d.http("DELETE", f"/v1/datasets/{ds_id}"), 401, "unauthorized", data)


def test_unexpected_errors_are_500_without_details(
    fake_qsv: Callable[[str], str],
    start_daemon: StartDaemon,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = start_daemon(fake_qsv(GENERIC))

    async def broken() -> dict[str, Any]:
        raise RuntimeError("boom at /secret/place")

    monkeypatch.setattr(d.daemon, "_status", broken)
    reply = _local(d, "GET", "/v1/status")
    assert reply.status == 500
    assert reply.json() == {
        "daemon_error": {"code": "internal", "message": "internal daemon error"}
    }
    assert "internal error" in caplog.text  # logged for the user, not sent to the client
    # the daemon keeps serving
    assert d.http("GET", "/v1/hello").status == 200


# -- pairing ---------------------------------------------------------------------------------


def test_pairing_deny_poll_and_local_requests(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(fake_qsv(GENERIC))
    _register(d, data)

    # a local client may ask to pair too; it is recorded as "local", not as a web origin
    local = _local(d, "POST", "/v1/pair", {"digest": _sha(data)}).json()
    pending = _local(d, "GET", "/v1/pairings").json()
    assert [(p["code"], p["origin"]) for p in pending] == [(local["code"], "local")]

    page = d.http("POST", "/v1/pair", {"digest": _sha(data)}).json()
    assert d.http("GET", f"/v1/pair/{page['request_id']}").json() == {"status": "pending"}
    denied = _local(d, "POST", f"/v1/pairings/{page['code']}/deny")
    assert denied.json() == {"denied": page["code"]}
    # a denied page hears so once, gets no token, and the request is then gone
    assert d.http("GET", f"/v1/pair/{page['request_id']}").json() == {"status": "denied"}
    assert d.http("GET", f"/v1/pair/{page['request_id']}").json() == {"status": "unknown"}
    assert d.http("GET", "/v1/pair/made-up").json() == {"status": "unknown"}

    _assert_refused(_local(d, "POST", "/v1/pairings/ZZZ-ZZZ/approve"), 404, "not_found")
    _assert_refused(_local(d, "POST", "/v1/pairings/ZZZ-ZZZ/deny"), 404, "not_found")
    # a page can't decide pairings, not even its own
    _assert_refused(d.http("POST", f"/v1/pairings/{local['code']}/approve"), 401, "unauthorized")


def test_pairing_requests_are_rate_limited(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(fake_qsv(GENERIC))
    _register(d, data)
    for _ in range(MAX_REQUESTS_PER_MINUTE):
        assert d.http("POST", "/v1/pair", {"digest": _sha(data)}).status == 200
    _assert_refused(d.http("POST", "/v1/pair", {"digest": _sha(data)}), 429, "rate_limited", data)


# -- browser tier: dataset actions -----------------------------------------------------------


def test_malformed_dataset_actions_are_refused_without_paths(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    log = tmp_path / "runs.log"
    d = start_daemon(_logging_qsv(fake_qsv, log, GENERIC))
    ds_id = _register(d, data, title="People")
    token = d.pair(_sha(data))
    log.unlink(missing_ok=True)

    def act(action: str, body: dict[str, Any]) -> Reply:
        return d.http("POST", f"/v1/datasets/{ds_id}/{action}", body, token=token)

    bad: list[tuple[str, dict[str, Any]]] = [
        ("rows", {"value": "alpha"}),
        ("rows", {"column": "", "value": "alpha"}),
        ("rows", {"column": 'na"me', "value": "alpha"}),
        ("rows", {"column": "name", "value": 5}),
        ("rows", {"limit": 0}),
        ("rows", {"limit": 501}),
        ("rows", {"offset": -1}),
        ("rows", {"offset": True}),
        ("query", {"command": 5}),
        ("query", {"command": "count", "args": "-s name"}),
        ("page", {"search": 5}),
        ("page", {"order": "name"}),
        ("page", {"order": {"column": 0, "desc": "yes"}}),
        ("page", {"order": {"column": "name"}}),
        ("page", {"start": -1}),
    ]
    for action, body in bad:
        reply = act(action, body)
        assert reply.status == 422, (action, body, reply.body)
        _assert_refused(reply, 422, "bad_request", data, tmp_path)
    assert not log.exists(), "qsv ran for a malformed request"

    # an unknown action is 404 only once the page is authorized for the dataset
    _assert_refused(act("delete", {}), 404, "not_found", data, tmp_path)
    no_token = d.http("POST", f"/v1/datasets/{ds_id}/delete", {})
    _assert_refused(no_token, 401, "unauthorized", data, tmp_path)


def test_query_with_empty_csv_output_has_no_table(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(fake_qsv(GENERIC))
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    reply = d.http(
        "POST",
        f"/v1/datasets/{ds_id}/query",
        {"command": "frequency", "args": ["-s", "name"]},
        token=token,
    )
    assert reply.status == 200, reply.body
    assert reply.json() == {"stdout": "", "truncated": False, "bytes": 0}


def test_render_answers_each_tier_differently(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    d = start_daemon(fake_qsv(GENERIC), browser_max_render=50)
    schematic = tmp_path / "s.html"
    ds_id = _register(d, data, schematic_path=str(schematic))
    token = d.pair(_sha(data))

    # the local user is told where the schematic went, not sent the page
    local = _local(d, "POST", f"/v1/datasets/{ds_id}/render", {})
    assert local.status == 200, local.body
    html = "<html>" + "x" * 100 + "</html>"
    assert local.json() == {"schematic_path": str(schematic), "bytes": len(html)}
    assert schematic.read_text() == html

    # a page gets the HTML itself, so it is capped; the refusal names no path
    browser = d.http("POST", f"/v1/datasets/{ds_id}/render", {}, token=token)
    _assert_refused(browser, 413, "too_large", data, schematic, tmp_path)


def test_render_with_viz_args_only_or_nothing_to_render(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    d = start_daemon(fake_qsv(GENERIC))
    ds_id = _register(d, data, viz_args=["--title", "People"])
    token = d.pair(_sha(data))
    page = d.http("POST", f"/v1/datasets/{ds_id}/render", {}, token=token)
    assert page.status == 200, page.body
    assert page.body == ("<html>" + "x" * 100 + "</html>").encode()
    assert page.headers["content-type"].startswith("text/html")
    # no schematic file to replace: the render's temporary file doesn't stay behind
    assert not list(d.daemon.home.glob(".qsv-clientd-*"))
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".html") == []

    other = tmp_path / "other.csv"
    other.write_text("x\n1\n")
    other_id = _register(d, other)
    other_token = d.pair(_sha(other))
    nothing = d.http("POST", f"/v1/datasets/{other_id}/render", {}, token=other_token)
    _assert_refused(nothing, 409, "cannot_render", other, tmp_path)


def test_browser_fetch_can_be_turned_off(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    log = tmp_path / "runs.log"
    d = start_daemon(_logging_qsv(fake_qsv, log, GENERIC), allow_fetch=False)
    ds_id = _register(d, data, source_url="https://example.com/data.csv")
    token = d.pair(_sha(data))
    log.unlink(missing_ok=True)
    reply = d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token)
    _assert_refused(reply, 403, "disabled", data, tmp_path)
    assert not log.exists()


def test_browser_run_timeout_is_a_scrubbed_qsv_error(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    body = """
import time
if sys.argv[1] == "count":
    time.sleep(30)
"""
    d = start_daemon(fake_qsv(body), browser_timeout=0.5, kill_grace=0.5)
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    started = time.monotonic()
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "count", "args": []}, token=token
    )
    assert time.monotonic() - started < 20
    _assert_refused(reply, 502, "qsv_timeout", data, tmp_path)
    assert d.daemon.running == 0  # its job slot was given back


def test_failed_fingerprint_at_registration_is_a_qsv_error(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    body = """
if sys.argv[1:] == ["--capabilities"]:
    print('{"binary": "qsv", "version": "25.0.0", "error_formats": ["text", "json"],'
          ' "commands": ["blake3"]}')
    sys.exit(0)
sys.stderr.write('{"error": {"kind": "io", "message": "cannot read input", "exit_code": 1,'
                 ' "command": "blake3"}}\\n')
sys.exit(1)
"""
    d = start_daemon(fake_qsv(body))
    reply = _local(d, "POST", "/v1/datasets", {"path": str(data)})
    _assert_refused(reply, 502, "qsv_io")
    assert reply.json()["daemon_error"]["message"] == "cannot read input"
    assert _local(d, "GET", "/v1/datasets").json() == []


# -- connections ------------------------------------------------------------------------------


def _local_run_request(d: DaemonThread, body: dict[str, Any]) -> bytes:
    payload = json.dumps(body).encode()
    return (
        f"POST /v1/run HTTP/1.1\r\nHost: 127.0.0.1:{d.port}\r\n"
        f"Authorization: Bearer {d.daemon.local_token}\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )


def _read_all(s: socket.socket) -> bytes:
    chunks = []
    while chunk := s.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


def test_bytes_after_the_body_are_not_a_disconnect(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    d = start_daemon(fake_qsv("import time\ntime.sleep(0.3)\nprint('done')\n"))
    with socket.create_connection(("127.0.0.1", d.port), timeout=30) as s:
        # trailing bytes (say, a pipelined request) arrive while qsv runs: only EOF cancels
        s.sendall(_local_run_request(d, {"command": "count"}) + b"GET /v1/hello HTTP/1.1\r\n")
        raw = _read_all(s)
    head, _, payload = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    result = json.loads(payload)["result"]
    assert result["exit_code"] == 0
    assert result["stdout_b64"] == "ZG9uZQo="  # "done\n"


def test_a_peer_that_stops_reading_is_dropped_after_the_flush_timeout(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsv_client.daemon._http import write_response as original

    async def buffered(writer: Any, response: Any) -> None:
        # let drain() return with the body still buffered, so the flush stalls in cleanup
        writer.transport.set_write_buffer_limits(high=1 << 30)
        await original(writer, response)

    monkeypatch.setattr(server, "write_response", buffered)
    monkeypatch.setattr(server, "_FLUSH_TIMEOUT", 0.3)
    d = start_daemon(fake_qsv('sys.stdout.write("x" * (32 << 20))\n'))
    with socket.create_connection(("127.0.0.1", d.port), timeout=30) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.sendall(_local_run_request(d, {"command": "count"}))
        s.recv(1024)  # the response has started; now stop reading
        deadline = time.monotonic() + 20
        while d.daemon._writers or d.daemon._tcp_open:
            assert time.monotonic() < deadline, "the stalled connection was never dropped"
            time.sleep(0.05)
    assert d.http("GET", "/v1/hello").status == 200


def test_shutdown_kills_an_in_flight_run(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    body = f"""
import time
open({str(pidfile)!r}, "w").write(str(os.getpid()))
time.sleep(60)
"""
    d = start_daemon(fake_qsv("import os\n" + body))
    with socket.create_connection(("127.0.0.1", d.port), timeout=30) as s:
        s.sendall(_local_run_request(d, {"command": "count"}))
        deadline = time.monotonic() + 20
        while not pidfile.exists() or not pidfile.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        pid = int(pidfile.read_text())
        started = time.monotonic()
        d.stop()
        assert time.monotonic() - started < 15
        # the connection ends without a reply (EOF, or a reset): shutdown doesn't wait for it
        # one recv: any byte of a reply fails, and only a reset with nothing read is accepted
        try:
            first = s.recv(65536)
        except ConnectionResetError:
            first = b""
        assert first == b"", first
    deadline = time.monotonic() + 20
    while _alive(pid):
        assert time.monotonic() < deadline, "qsv kept running after the daemon stopped"
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -- lifecycle (on the test's own event loop) -----------------------------------------------


async def _unix_get(path: Path, route: str) -> tuple[int, Any]:
    if sys.platform == "win32":
        raise AssertionError("Unix socket")
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(f"GET {route} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


async def test_unix_only_daemon_replaces_a_stale_socket(
    fake_qsv: Callable[[str], str], home: Path
) -> None:
    if sys.platform == "win32":
        pytest.skip("Unix socket")
    sock_path = home / "q.sock"
    # a socket file left by a daemon that died: nothing listens on it any more
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()
    assert sock_path.exists()
    # config port 0 and no override: no loopback listener at all
    daemon = Daemon(
        DaemonConfig(binary=fake_qsv(GENERIC), port=0), home=home, socket_path=sock_path
    )
    await daemon.start()
    try:
        assert daemon.port is None
        status, body = await _unix_get(sock_path, "/v1/status")
        assert status == 200
        assert body["port"] is None and body["socket"] == str(sock_path)
    finally:
        await daemon.close()
    assert not sock_path.exists()


async def test_bind_errors_other_than_a_long_path_propagate(
    fake_qsv: Callable[[str], str], home: Path
) -> None:
    if sys.platform == "win32":
        pytest.skip("Unix socket")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    locked = home / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        daemon = Daemon(
            DaemonConfig(binary=fake_qsv(GENERIC)), home=home, socket_path=locked / "q.sock"
        )
        with pytest.raises(PermissionError):
            await daemon.start()
        await daemon.close()
        assert list(locked.iterdir()) == []
    finally:
        locked.chmod(0o700)


async def test_closing_an_unstarted_daemon_leaves_results_alone(
    fake_qsv: Callable[[str], str], home: Path
) -> None:
    # e.g. a second instance that never bound: the running daemon's results are not its own
    kept = home / "results" / "local" / "result.csv"
    kept.parent.mkdir(parents=True)
    kept.write_text("a\n1\n")
    await Daemon(DaemonConfig(binary=fake_qsv(GENERIC)), home=home).close()
    assert kept.exists()


async def test_serve_forever_cleans_up_on_sigterm(
    fake_qsv: Callable[[str], str], home: Path
) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    import signal

    daemon = Daemon(DaemonConfig(binary=fake_qsv(GENERIC)), home=home, tcp_port=0)
    task = asyncio.create_task(daemon.serve_forever())
    deadline = time.monotonic() + 30
    # serve_forever installs its handlers right after start(), with no await in between
    while not daemon._started:
        assert time.monotonic() < deadline and not task.done()
        await asyncio.sleep(0.02)
    sock_path = daemon.socket_path
    token_file = home / "run" / "local-token"
    assert sock_path.exists() and token_file.exists()
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 30)
    assert not sock_path.exists()
    assert not token_file.exists() and not (home / "run" / "daemon.json").exists()


def test_remove_runtime_files(home: Path) -> None:
    run = home / "run"
    run.mkdir()
    (run / "local-token").write_text("t")
    server.remove_runtime_files()
    assert not run.exists()
    server.remove_runtime_files()  # nothing left to remove: still fine
