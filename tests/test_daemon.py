"""qsv-clientd end to end with a fake qsv: local runs, the browser tier, pairing, cancellation."""

from __future__ import annotations

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

import pytest
from daemon_helpers import DaemonThread

from qsv_client import QsvCsvError, QsvDaemonError, QsvDaemonUnavailable, QsvTimeout
from qsv_client.daemon import AsyncDaemonClient, DaemonClient, DaemonConfig
from qsv_client.daemon.server import Daemon

StartDaemon = Callable[..., DaemonThread]

ECHO = """
import json
print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read()}))
"""


@pytest.fixture
def start_daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartDaemon]:
    # short path: macOS limits Unix socket paths to 104 bytes, too short for pytest's tmp_path
    home = Path(tempfile.mkdtemp(prefix="qcd"))
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(home))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    started: list[DaemonThread] = []

    def start(binary: str, **settings: object) -> DaemonThread:
        config = DaemonConfig(binary=binary, **settings)  # type: ignore[arg-type]
        # a second daemon in one test gets its own socket; clients use the first one's
        socket_path = home / f"s{len(started)}.sock" if started else None
        daemon = Daemon(config, home=home, tcp_port=0, socket_path=socket_path)
        thread = DaemonThread(daemon).start()
        started.append(thread)
        return thread

    yield start
    for thread in started:
        thread.stop()
    shutil.rmtree(home, ignore_errors=True)


def _script(fake_qsv: Callable[[str], str], body: str) -> str:
    return fake_qsv("import os\n" + body)


# -- local tier -----------------------------------------------------------------------------


def test_run_round_trip(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    binary = _script(fake_qsv, ECHO)
    start_daemon(binary)
    client = DaemonClient(cwd=tmp_path)
    result = client.run("count", "data.csv", 3, stdin="a,b\n")
    payload = json.loads(result.stdout or "")
    assert payload["argv"] == ["count", "data.csv", "3"]
    assert Path(payload["cwd"]).resolve() == tmp_path.resolve()
    assert payload["stdin"] == "a,b\n"
    assert result.args[1:] == ("count", "data.csv", "3")
    assert result.ok


async def test_async_run_round_trip(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    start_daemon(_script(fake_qsv, ECHO))
    result = await AsyncDaemonClient(cwd=tmp_path).run("headers", stdin=b"x\n", text=False)
    assert result.stdout is None
    assert json.loads(result.stdout_bytes or b"")["stdin"] == "x\n"


def test_stdout_path_is_relative_to_cwd(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    start_daemon(_script(fake_qsv, ECHO))
    result = DaemonClient(cwd=tmp_path).run("count", stdout_path="out.json")
    assert result.stdout_path == tmp_path / "out.json"
    assert json.loads((tmp_path / "out.json").read_text())["argv"] == ["count"]


CSV_ERROR = """
sys.stderr.write('{"error":{"kind":"csv","level":"error","message":"bad row","exit_code":1,'
                 '"command":"count","qsv_version":"25.0.0"}}\\n')
sys.exit(1)
"""


def test_errors_are_the_same_classes(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    start_daemon(_script(fake_qsv, CSV_ERROR))
    client = DaemonClient()
    with pytest.raises(QsvCsvError, match="bad row") as info:
        client.run("count", "x.csv")
    assert info.value.structured
    result = client.run("count", "x.csv", check=False)
    assert result.exit_code == 1


async def test_async_errors_are_the_same_classes(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    start_daemon(_script(fake_qsv, CSV_ERROR))
    with pytest.raises(QsvCsvError):
        await AsyncDaemonClient().run("count")


def test_timeout(fake_qsv: Callable[[str], str], start_daemon: StartDaemon) -> None:
    start_daemon(_script(fake_qsv, "import time; time.sleep(30)\n"))
    started = time.monotonic()
    with pytest.raises(QsvTimeout):
        DaemonClient().run("count", timeout=0.5, check=False)
    assert time.monotonic() - started < 20


def test_no_daemon() -> None:
    with pytest.raises(QsvDaemonUnavailable):
        DaemonClient(socket_path="/nonexistent/qsvd.sock").run("count")
    assert not DaemonClient(socket_path="/nonexistent/qsvd.sock").available()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket / POSIX signals")
def test_socket_is_owner_only(fake_qsv: Callable[[str], str], start_daemon: StartDaemon) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    sock = Path(d.daemon._socket_path)
    assert sock.stat().st_mode & 0o777 == 0o600
    assert sock.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket / POSIX signals")
def test_client_disconnect_kills_the_run(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    d = start_daemon(
        _script(
            fake_qsv,
            f"""
import time
open({str(pidfile)!r}, "w").write(str(os.getpid()))
time.sleep(60)
""",
        )
    )
    if sys.platform == "win32":  # also tells mypy --platform win32 the rest is POSIX-only
        pytest.skip("Unix socket")
    body = json.dumps({"command": "count", "args": []}).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(d.daemon._socket_path))
    s.sendall(b"POST /v1/run HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    deadline = time.monotonic() + 20
    while not pidfile.exists() or not pidfile.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    s.close()
    deadline = time.monotonic() + 20
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "qsv kept running after the client left"
        time.sleep(0.05)


# -- browser tier ---------------------------------------------------------------------------


@pytest.fixture
def data(tmp_path: Path) -> Path:
    path = tmp_path / "data.csv"
    path.write_text("name,n\nalpha,1\nbeta,2\n")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _register(d: DaemonThread, path: Path, **extra: object) -> str:
    reply = d.http(
        "POST",
        "/v1/datasets",
        {"path": str(path), **extra},
        origin=None,
        token=d.daemon.local_token,
    )
    assert reply.status == 200, reply.body
    dataset_id: str = reply.json()["id"]
    return dataset_id


def test_hello_reveals_only_the_service(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    _register(d, data)
    reply = d.http("GET", "/v1/hello")
    assert reply.json() == {"service": "qsv-clientd", "api": 1}
    assert reply.headers["access-control-allow-origin"] == "null"


def test_dns_rebinding_host_is_refused(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    assert d.http("GET", "/v1/hello", host=f"evil.example:{d.port}").status == 421
    assert d.http("GET", "/v1/hello", host=f"localhost:{d.port}").status == 200


def test_preflight(fake_qsv: Callable[[str], str], start_daemon: StartDaemon) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    reply = d.http(
        "OPTIONS",
        "/v1/pair",
        headers={
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert reply.status == 204
    assert reply.headers["access-control-allow-origin"] == "null"
    assert "Authorization" in reply.headers["access-control-allow-headers"]
    assert reply.headers["access-control-allow-private-network"] == "true"


def test_browser_cannot_use_local_routes(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    assert d.http("POST", "/v1/run", {"command": "count"}).status == 401
    assert d.http("GET", "/v1/status").status == 401
    assert d.http("GET", "/v1/datasets").status == 401
    # the local token is not enough when the request comes from a page
    stolen = d.http("POST", "/v1/run", {"command": "count"}, token=d.daemon.local_token)
    assert stolen.status == 401
    local = d.http("POST", "/v1/run", {"command": "count"}, origin=None, token=d.daemon.local_token)
    assert local.status == 200


def test_only_allowed_origins_may_pair(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    _register(d, data)
    reply = d.http("POST", "/v1/pair", {"sha256": _sha(data)}, origin="https://evil.example")
    assert reply.status == 403
    off = start_daemon(_script(fake_qsv, ECHO), browser=False)
    assert off.http("POST", "/v1/pair", {"sha256": _sha(data)}).status == 403


def test_pair_answer_does_not_reveal_registration(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    _register(d, data)
    known = d.http("POST", "/v1/pair", {"sha256": _sha(data)}).json()
    unknown = d.http("POST", "/v1/pair", {"sha256": "0" * 64}).json()
    assert set(known) == set(unknown)
    approve = d.http(
        "POST", f"/v1/pairings/{unknown['code']}/approve", origin=None, token=d.daemon.local_token
    )
    assert approve.status == 409


def test_paired_token_is_scoped_to_its_dataset(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    ds_id = _register(d, data)
    other = tmp_path / "other.csv"
    other.write_text("x\n1\n")
    other_id = _register(d, other)
    assert d.http("GET", f"/v1/datasets/{ds_id}").status == 401
    token = d.pair(_sha(data))
    info = d.http("GET", f"/v1/datasets/{ds_id}", token=token).json()
    assert info["id"] == ds_id
    assert "path" not in info and str(data) not in json.dumps(info)
    assert d.http("GET", f"/v1/datasets/{other_id}", token=token).status == 401
    revoke = d.http("POST", "/v1/tokens/revoke", {}, origin=None, token=d.daemon.local_token)
    assert revoke.json()["revoked"] == 1
    assert d.http("GET", f"/v1/datasets/{ds_id}", token=token).status == 401


def test_query_is_allowlisted_and_scrubbed(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    ok = d.http(
        "POST",
        f"/v1/datasets/{ds_id}/query",
        {"command": "frequency", "args": ["-s", "name"]},
        token=token,
    )
    assert ok.status == 200
    argv = json.loads(ok.json()["stdout"])["argv"]
    assert argv == ["frequency", "-s", "name", str(data.resolve())]
    for command, args in (
        ("sqlp", ["select 1"]),
        ("slice", ["-o", "/tmp/x"]),
        ("count", ["/etc/passwd"]),
    ):
        bad = d.http(
            "POST", f"/v1/datasets/{ds_id}/query", {"command": command, "args": args}, token=token
        )
        assert bad.status == 403, (command, bad.body)


def test_qsv_errors_reach_browsers_without_paths(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    body = """
sys.stderr.write(json.dumps({"error": {"kind": "io", "message": "cannot open " + sys.argv[-1],
                                       "exit_code": 1, "command": sys.argv[1]}}) + "\\n")
sys.exit(1)
"""
    d = start_daemon(_script(fake_qsv, "import json\n" + body))
    ds_id = _register(d, data, title="My data")
    token = d.pair(_sha(data))
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "count", "args": []}, token=token
    )
    assert reply.status == 502
    assert str(data.resolve()) not in reply.body.decode()
    assert "My data" in reply.json()["daemon_error"]["message"]


def test_registration_checks_schematic_fingerprint(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    html = tmp_path / "s.html"
    source = {
        "schematic_uuid": "u-9",
        "fingerprint": {"algo": "sha256", "digest": "0" * 64, "bytes": 1},
    }
    html.write_text(
        f'<script type="application/json" id="qsv-schematic-source">{json.dumps(source)}</script>'
    )
    reply = d.http(
        "POST",
        "/v1/datasets",
        {"path": str(data), "schematic_path": str(html)},
        origin=None,
        token=d.daemon.local_token,
    )
    assert reply.status == 409
    source["fingerprint"] = {"algo": "sha256", "digest": _sha(data), "bytes": 1}
    html.write_text(
        f'<script type="application/json" id="qsv-schematic-source">{json.dumps(source)}</script>'
    )
    ds_id = _register(d, data, schematic_path=str(html))
    paired = d.http("POST", "/v1/pair", {"schematic_uuid": "u-9"}).json()
    approve = d.http(
        "POST", f"/v1/pairings/{paired['code']}/approve", origin=None, token=d.daemon.local_token
    )
    assert approve.json()["dataset_id"] == ds_id


def test_daemon_errors_raise_daemon_error(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    start_daemon(_script(fake_qsv, ECHO))
    with pytest.raises(QsvDaemonError) as info:
        DaemonClient().request("POST", "/v1/datasets", {"path": "relative.csv"})
    assert info.value.status == 422


def test_cors_only_for_allowed_origins(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    other = d.http("GET", "/v1/hello", origin="https://evil.example")
    assert "access-control-allow-origin" not in other.headers
    lna = d.http("OPTIONS", "/v1/pair", headers={"Access-Control-Request-Local-Network": "true"})
    assert lna.headers["access-control-allow-local-network"] == "true"


def test_render_passes_the_daemon_port(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    body = """
out = sys.argv[sys.argv.index("--output") + 1]
open(out, "w").write("<html>port=" + os.environ.get("QSV_CLIENTD_PORT", "") + "</html>")
"""
    d = start_daemon(_script(fake_qsv, body))
    ds_id = _register(d, data, schematic_path=str(tmp_path / "s.html"))
    token = d.pair(_sha(data))
    page = d.http("POST", f"/v1/datasets/{ds_id}/render", {}, token=token)
    assert page.body == f"<html>port={d.port}</html>".encode()


def test_client_uses_configured_socket(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(os.environ["QSV_CLIENTD_HOME"])
    sock = home / "custom.sock"
    (home / "daemon.json").write_text(json.dumps({"socket": str(sock)}))
    start_daemon(_script(fake_qsv, ECHO), socket=str(sock))
    if sys.platform != "win32":
        assert DaemonClient()._target.socket_path == str(sock)
    assert DaemonClient().available()
