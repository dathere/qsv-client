"""qsv-clientd end to end with a fake qsv: local runs, the browser tier, pairing, cancellation."""

from __future__ import annotations

import asyncio
import contextlib
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
from daemon_helpers import DaemonThread

from qsv_client import QsvCsvError, QsvDaemonError, QsvDaemonUnavailable, QsvTimeout
from qsv_client.daemon import AsyncDaemonClient, DaemonClient, DaemonConfig
from qsv_client.daemon.server import Daemon

StartDaemon = Callable[..., DaemonThread]

ECHO = """
import contextlib
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
    reply = d.http("POST", "/v1/pair", {"digest": _sha(data)}, origin="https://evil.example")
    assert reply.status == 403
    off = start_daemon(_script(fake_qsv, ECHO), browser=False)
    assert off.http("POST", "/v1/pair", {"digest": _sha(data)}).status == 403


def test_pair_answer_does_not_reveal_registration(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    _register(d, data)
    known = d.http("POST", "/v1/pair", {"digest": _sha(data)}).json()
    unknown = d.http("POST", "/v1/pair", {"digest": "0" * 64}).json()
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


B3_QSV = """
import hashlib
if sys.argv[1:] == ["--capabilities"]:
    print('{"binary": "qsv", "version": "25.0.0", "error_formats": ["text", "json"],'
          ' "commands": ["blake3", "count"]}')
    sys.exit(0)
if sys.argv[1] == "blake3":
    assert sys.argv[2] == "--no-names", sys.argv
    print(hashlib.blake2s(open(sys.argv[3], "rb").read()).hexdigest())  # stands in for BLAKE3
elif sys.argv[1] == "count":
    print(sum(1 for _ in open(sys.argv[-1])) - 1)
"""


def _b3(path: Path) -> str:
    return hashlib.blake2s(path.read_bytes()).hexdigest()


def _write_page(html: Path, fingerprint: dict[str, object]) -> None:
    source = json.dumps({"schematic_uuid": "u-b3", "fingerprint": fingerprint})
    html.write_text(f'<script type="application/json" id="qsv-schematic-source">{source}</script>')


def test_fingerprints_with_qsv_blake3(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    d = start_daemon(_script(fake_qsv, B3_QSV))
    html = tmp_path / "s.html"
    # a page fingerprinted with another algorithm is checked in that algorithm
    _write_page(html, {"algo": "sha256", "digest": "0" * 64, "bytes": 1})
    stale = d.http(
        "POST",
        "/v1/datasets",
        {"path": str(data), "schematic_path": str(html)},
        origin=None,
        token=d.daemon.local_token,
    )
    assert stale.status == 409
    _write_page(html, {"algo": "sha256", "digest": _sha(data), "bytes": 1})
    ds_id = _register(d, data, schematic_path=str(html))

    token = d.pair(_b3(data))
    info = d.http("GET", f"/v1/datasets/{ds_id}", token=token).json()
    size = data.stat().st_size
    assert info["fingerprint"] == {"algo": "blake3", "digest": _b3(data), "bytes": size}
    fresh = d.http("POST", f"/v1/datasets/{ds_id}/refresh", {}, token=token).json()
    assert fresh["changed"] is False and fresh["fingerprint"] == info["fingerprint"]
    with data.open("a") as fh:
        fh.write("gamma,3\n")
    edited = d.http("POST", f"/v1/datasets/{ds_id}/refresh", {}, token=token).json()
    assert edited["changed"] is True and edited["fingerprint"]["digest"] == _b3(data)

    # a qsv without `blake3` falls back to SHA-256, and can't check a BLAKE3 page
    lite = start_daemon(_script(fake_qsv, ECHO))
    _write_page(html, {"algo": "blake3", "digest": _b3(data), "bytes": 1})
    unverifiable = lite.http(
        "POST",
        "/v1/datasets",
        {"path": str(data), "schematic_path": str(html)},
        origin=None,
        token=lite.daemon.local_token,
    )
    assert unverifiable.status == 501, unverifiable.body
    plain = tmp_path / "plain.csv"
    plain.write_text("x\n1\n")
    lite_id = _register(lite, plain)
    lite_token = lite.pair(_sha(plain))
    lite_info = lite.http("GET", f"/v1/datasets/{lite_id}", token=lite_token).json()
    assert lite_info["fingerprint"]["algo"] == "sha256"


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


# -- regressions (review 5501) --------------------------------------------------------------

OK_QSV = """
if "--output" in sys.argv:
    open(sys.argv[sys.argv.index("--output") + 1], "w").write("<html></html>")
print("1")
"""


def test_second_daemon_keeps_the_first_ones_token(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    if sys.platform == "win32":
        pytest.skip("Unix socket")
    first = start_daemon(_script(fake_qsv, ECHO))
    token_file = Path(os.environ["QSV_CLIENTD_HOME"]) / "run" / "local-token"
    assert token_file.read_text() == first.daemon.local_token
    second = DaemonThread(
        Daemon(DaemonConfig(binary=first.daemon.binary), home=first.daemon.home, tcp_port=0)
    )
    with pytest.raises(RuntimeError, match="already listening"):
        second.start()
    assert token_file.read_text() == first.daemon.local_token
    assert first.daemon._socket_path.exists()
    assert DaemonClient().available()


def test_rows_respect_the_output_cap_and_long_fields(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    big = "x" * (200 * 1024)
    body = f"""
if sys.argv[1] == "count":
    print(1)
    sys.exit(0)
out = sys.argv[sys.argv.index("--output") + 1] if "--output" in sys.argv else None
text = "name\\n{big}\\n"
if out: open(out, "w").write(text)
else: sys.stdout.write(text)
"""
    data = tmp_path / "d.csv"
    data.write_text("name\n1\n")
    d = start_daemon(_script(fake_qsv, body))
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    ok = d.http("POST", f"/v1/datasets/{ds_id}/rows", {}, token=token)
    assert ok.status == 200, ok.body[:200]
    assert ok.json()["rows"] == [[big]]
    small = start_daemon(_script(fake_qsv, body), browser_max_output=1024)
    ds2 = _register(small, data)
    token2 = small.pair(_sha(data))
    capped = small.http("POST", f"/v1/datasets/{ds2}/rows", {}, token=token2)
    assert capped.status == 413
    stats = small.http("POST", f"/v1/datasets/{ds2}/refresh", {"stats": True}, token=token2)
    assert stats.status == 413


def test_viz_rerender_replaces_a_stale_schematic(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path, tmp_path: Path
) -> None:
    d = start_daemon(_script(fake_qsv, OK_QSV))
    html = tmp_path / "s.html"
    stale = {"fingerprint": {"algo": "sha256", "digest": "0" * 64, "bytes": 1}}
    html.write_text(
        f'<script type="application/json" id="qsv-schematic-source">{json.dumps(stale)}</script>'
    )
    reply = d.http(
        "POST",
        "/v1/datasets",
        {"path": str(data), "schematic_path": str(html), "render": True},
        origin=None,
        token=d.daemon.local_token,
    )
    assert reply.status == 200, reply.body
    assert html.read_text() == "<html></html>"


def test_relative_cwd_is_relative_to_the_caller(
    fake_qsv: Callable[[str], str],
    start_daemon: StartDaemon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_daemon(_script(fake_qsv, ECHO))
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    payload = json.loads(DaemonClient(cwd="sub").run("count").stdout or "")
    assert Path(payload["cwd"]).resolve() == (tmp_path / "sub").resolve()


def test_qsv_or_daemon_never_drops_options(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    from qsv_client import Qsv
    from qsv_client.daemon import qsv_or_daemon

    binary = _script(fake_qsv, ECHO)
    start_daemon(binary)
    via_daemon = qsv_or_daemon(cwd=tmp_path)
    assert isinstance(via_daemon, DaemonClient) and via_daemon.cwd == str(tmp_path)
    assert isinstance(qsv_or_daemon(binary, timeout=5), Qsv)
    assert isinstance(qsv_or_daemon(timeout=5, binary=binary), Qsv)


def test_cli_run_forwards_stdin(fake_qsv: Callable[[str], str], start_daemon: StartDaemon) -> None:
    import subprocess

    start_daemon(_script(fake_qsv, ECHO))

    def run(*argv: str) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "qsv_client.daemon", "run", *argv],
            input=b"a\n1\n",
            capture_output=True,
            timeout=60,
            check=True,
        )
        stdin: str = json.loads(proc.stdout)["stdin"]
        return stdin

    assert run("--stdin", "count") == "a\n1\n"
    assert run("count", "-") == "a\n1\n"
    # without being asked, stdin is left for the caller (e.g. a `while read` loop)
    assert run("count") == ""


def test_queued_run_times_out_as_qsv_timeout(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    import threading

    start_daemon(_script(fake_qsv, "import time; time.sleep(3)\n"), max_jobs=1)
    first = threading.Thread(target=lambda: DaemonClient().run("count", timeout=30))
    first.start()
    time.sleep(0.5)  # let it take the only job slot
    try:
        with pytest.raises(QsvTimeout, match="job slots"):
            DaemonClient().run("count", timeout=0.5)
    finally:
        first.join()


def test_os_errors_are_the_same_as_locally(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    from qsv_client import Qsv

    binary = _script(fake_qsv, ECHO)
    start_daemon(binary)
    target = tmp_path  # a directory: IsADirectoryError, or PermissionError on Windows
    with pytest.raises(OSError) as local:
        Qsv(binary).run("count", stdout_path=target)
    with pytest.raises(OSError) as remote:
        DaemonClient().run("count", stdout_path=target)
    assert type(remote.value) is type(local.value)
    assert remote.value.errno == local.value.errno


ENV_QSV = """
print(os.environ.get("QSV_STATSCACHE_MODE", "unset"))
"""


def test_queries_never_use_the_stats_cache(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(_script(fake_qsv, ENV_QSV))
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "frequency", "args": []}, token=token
    )
    assert reply.status == 200, reply.body
    assert reply.json()["stdout"].strip() == "none"


class _Server:
    """A local HTTP server serving one CSV, slowly enough for fetches to overlap."""

    def __init__(self, body: bytes) -> None:
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for i in range(0, len(body), 4096):
                    self.wfile.write(body[i : i + 4096])
                    time.sleep(0.002)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/data.csv"

    def close(self) -> None:
        self.httpd.shutdown()


def test_concurrent_fetches_and_replace_keeps_mode(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    body = b"name\n" + b"".join(b"row%d\n" % i for i in range(20000))
    server = _Server(body)
    try:
        data = tmp_path / "d.csv"
        data.write_text("name\nold\n")
        data.chmod(0o600)
        d = start_daemon(_script(fake_qsv, OK_QSV))
        ds_id = _register(d, data, source_url=server.url, allow_replace=True)
        token = d.pair(_sha(data))
        with ThreadPoolExecutor(4) as pool:
            replies = list(
                pool.map(
                    lambda _: d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token),
                    range(4),
                )
            )
        assert [r.status for r in replies] == [200] * 4, [r.body for r in replies]
        assert data.read_bytes() == body
        if sys.platform != "win32":
            assert data.stat().st_mode & 0o777 == 0o600
        leftovers = [p.name for p in data.parent.iterdir() if p.name.startswith(".qsv-clientd")]
        assert leftovers == []
    finally:
        server.close()


HEADER_QSV = """
import csv, io
csv.field_size_limit(1 << 30)
records = list(csv.reader(open(sys.argv[-1], newline=""))) if len(sys.argv) > 2 else []
if sys.argv[1] == "slice":  # slice --len 0 <path>: the header record, quoted fields intact
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\\n").writerow(records[0])
    sys.stdout.buffer.write(buf.getvalue().encode())
elif sys.argv[1] == "count" and len({len(r) for r in records}) > 1:
    sys.stderr.write("csv error: found record with 1 fields, but the previous record has 2\\n")
    sys.exit(1)
else:
    print("1")
"""


def test_fetch_keeps_data_when_columns_are_missing(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = tmp_path / "d.csv"
    data.write_text("name,team\nold,red\n")
    error_page = _Server(b"Service Unavailable - try again later\n")
    try:
        d = start_daemon(_script(fake_qsv, HEADER_QSV))
        ds_id = _register(d, data, source_url=error_page.url, allow_replace=True)
        token = d.pair(_sha(data))
        reply = d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token)
        assert reply.status == 422 and reply.json()["daemon_error"]["code"] == "columns_missing"
        assert data.read_text() == "name,team\nold,red\n"
        # a page can't override the check
        forced = d.http("POST", f"/v1/datasets/{ds_id}/fetch", {"force": True}, token=token)
        assert forced.status == 422
    finally:
        error_page.close()
    # a new column is fine
    grown = _Server(b"name,team,score\nnew,blue,3\n")
    try:
        d.http(
            "POST",
            "/v1/datasets",
            {"path": str(data), "source_url": grown.url, "allow_replace": True},
            origin=None,
            token=d.daemon.local_token,
        )
        reply = d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token)
        assert reply.status == 200, reply.body
        assert data.read_text() == "name,team,score\nnew,blue,3\n"
    finally:
        grown.close()
    # a rename needs the user's say-so
    renamed = _Server(b"who,team,score\nnew,blue,3\n")
    try:
        d.http(
            "POST",
            "/v1/datasets",
            {"path": str(data), "source_url": renamed.url, "allow_replace": True},
            origin=None,
            token=d.daemon.local_token,
        )
        assert d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token).status == 422
        reply = d.http(
            "POST",
            f"/v1/datasets/{ds_id}/fetch",
            {"force": True},
            origin=None,
            token=d.daemon.local_token,
        )
        assert reply.status == 200, reply.body
        assert data.read_text().startswith("who,")
    finally:
        renamed.close()


def test_fetch_keeps_data_when_records_are_ragged(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = tmp_path / "d.csv"
    data.write_text("name,team\nold,red\n")
    ragged = _Server(b"name,team\nnew,blue\nbroken\n")  # matching header, bad record later
    try:
        d = start_daemon(_script(fake_qsv, HEADER_QSV))
        ds_id = _register(d, data, source_url=ragged.url, allow_replace=True)
        reply = d.http(
            "POST",
            f"/v1/datasets/{ds_id}/fetch",
            {"force": True},  # force skips only the column check
            origin=None,
            token=d.daemon.local_token,
        )
        assert reply.status != 200, reply.body
        assert data.read_text() == "name,team\nold,red\n"
    finally:
        ragged.close()


def test_fetch_handles_a_long_header(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    long_name = "x" * 200_000  # over csv's default 128 KiB field limit
    data = tmp_path / "d.csv"
    data.write_text(f"name,{long_name}\nold,red\n")
    body = f"name,{long_name}\nnew,blue\n".encode()
    server = _Server(body)
    try:
        d = start_daemon(_script(fake_qsv, HEADER_QSV))
        ds_id = _register(d, data, source_url=server.url, allow_replace=True)
        token = d.pair(_sha(data))
        reply = d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token)
        assert reply.status == 200, reply.body[:500]
        assert data.read_bytes() == body
    finally:
        server.close()


def test_fetch_compares_header_line_breaks_exactly(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    original = b'"team\r\nname",x\nold,1\n'
    data = tmp_path / "d.csv"
    data.write_bytes(original)
    server = _Server(b'"team\nname",x\nnew,2\n')  # a different column name, by one CR
    try:
        d = start_daemon(_script(fake_qsv, HEADER_QSV))
        ds_id = _register(d, data, source_url=server.url, allow_replace=True)
        token = d.pair(_sha(data))
        reply = d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token)
        assert reply.status == 422 and reply.json()["daemon_error"]["code"] == "columns_missing"
        assert data.read_bytes() == original
    finally:
        server.close()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_cwd_keeps_dotdot_after_symlinks(
    fake_qsv: Callable[[str], str],
    start_daemon: StartDaemon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_daemon(_script(fake_qsv, ECHO))
    (tmp_path / "data" / "sub").mkdir(parents=True)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "link").symlink_to(tmp_path / "data" / "sub")
    monkeypatch.chdir(tmp_path / "work")
    payload = json.loads(DaemonClient(cwd="link/..").run("count").stdout or "")
    assert Path(payload["cwd"]).resolve() == (tmp_path / "data").resolve()


def test_cancelled_fetch_leaves_no_staging_files(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    body = b"name\n" + b"".join(b"row%d\n" % i for i in range(60000))
    server = _Server(body)
    try:
        data = tmp_path / "d.csv"
        data.write_text("name\nold\n")
        d = start_daemon(_script(fake_qsv, OK_QSV))
        ds_id = _register(d, data, source_url=server.url)
        token = d.pair(_sha(data))
        req = json.dumps({}).encode()
        s = socket.create_connection(("127.0.0.1", d.port))
        s.sendall(
            f"POST /v1/datasets/{ds_id}/fetch HTTP/1.1\r\nHost: 127.0.0.1:{d.port}\r\n"
            f"Origin: null\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Length: {len(req)}\r\n\r\n".encode()
            + req
        )
        time.sleep(0.2)  # mid-download
        s.close()
        cache = d.daemon.home / "cache" / ds_id
        deadline = time.monotonic() + 30
        while any(p.name.startswith(".qsv-clientd") for p in cache.iterdir()):
            assert time.monotonic() < deadline, list(cache.iterdir())
            time.sleep(0.1)
        # the next fetch is not overtaken by the cancelled one
        again = d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token)
        assert again.status == 200, again.body
        assert [p.name for p in cache.iterdir()] == ["d.csv"]
    finally:
        server.close()


def test_sigterm_shuts_down_cleanly(fake_qsv: Callable[[str], str]) -> None:
    import signal
    import subprocess

    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    home = Path(tempfile.mkdtemp(prefix="qcd"))
    try:
        env = {**os.environ, "QSV_CLIENTD_HOME": str(home), "QSV_BIN": _script(fake_qsv, ECHO)}
        env.pop("QSV_CLIENTD_SOCKET", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "qsv_client.daemon", "serve", "--port", "0"], env=env
        )
        run = home / "run"
        deadline = time.monotonic() + 30
        while not (run / "daemon.json").exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
        assert sorted(p.name for p in run.iterdir()) == []
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_shutdown_is_not_blocked_by_an_idle_connection(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    d = start_daemon(_script(fake_qsv, ECHO))
    idle = socket.create_connection(("127.0.0.1", d.port))
    idle.sendall(b"GET /v1/hel")  # never finishes its request head
    time.sleep(0.2)
    started = time.monotonic()
    d.stop()
    assert time.monotonic() - started < 5
    idle.close()


def test_slow_request_head_times_out(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsv_client.daemon import server

    monkeypatch.setattr(server, "_HEAD_TIMEOUT", 0.3)
    d = start_daemon(_script(fake_qsv, ECHO))
    s = socket.create_connection(("127.0.0.1", d.port), timeout=10)
    s.sendall(b"GET /v1/hello HTTP/1.1\r\n")
    reply = s.recv(4096)
    s.close()
    assert reply.startswith(b"HTTP/1.1 408")


def test_slow_request_body_times_out(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsv_client.daemon import server

    monkeypatch.setattr(server, "_BODY_TIMEOUT", 0.3)
    d = start_daemon(_script(fake_qsv, ECHO))
    s = socket.create_connection(("127.0.0.1", d.port), timeout=10)
    s.sendall(
        b"GET /v1/hello HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 10\r\n\r\n" % d.port
    )
    reply = s.recv(4096)
    s.close()
    assert reply.startswith(b"HTTP/1.1 408")


def test_loopback_connections_are_capped(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsv_client.daemon import server

    monkeypatch.setattr(server, "_MAX_TCP_CONNECTIONS", 2)
    d = start_daemon(_script(fake_qsv, ECHO))
    held = [socket.create_connection(("127.0.0.1", d.port), timeout=10) for _ in range(2)]
    try:
        time.sleep(0.3)  # let the daemon accept them
        extra = socket.create_connection(("127.0.0.1", d.port), timeout=10)
        # dropped at once, without waiting for a request
        with contextlib.suppress(ConnectionResetError):
            assert extra.recv(4096) == b""
        extra.close()
        if sys.platform != "win32":  # the owner's socket clients are unaffected
            assert DaemonClient().run("count").exit_code == 0
    finally:
        for s in held:
            s.close()
    time.sleep(0.3)
    assert d.http("GET", "/v1/hello").status == 200


def test_shutdown_is_not_blocked_by_a_client_that_stops_reading(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon
) -> None:
    # 32 MiB of stdout: far more than the socket buffers hold, so the write blocks
    d = start_daemon(_script(fake_qsv, 'sys.stdout.write("x" * (32 << 20))\n'))
    body = json.dumps({"command": "count"}).encode()
    s = socket.create_connection(("127.0.0.1", d.port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.sendall(
        f"POST /v1/run HTTP/1.1\r\nHost: 127.0.0.1:{d.port}\r\n"
        f"Authorization: Bearer {d.daemon.local_token}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    s.recv(1024)  # the response has started; now stop reading
    started = time.monotonic()
    d.stop()
    assert time.monotonic() - started < 10
    s.close()


def test_shutdown_aborts_a_connection_stuck_flushing_in_cleanup(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsv_client.daemon import server
    from qsv_client.daemon._http import write_response as original

    async def buffered(writer: Any, response: Any) -> None:
        # let drain() return with the whole body still buffered, so the connection reaches its
        # cleanup (close + wait_closed) while the flush is stalled on a peer that won't read
        writer.transport.set_write_buffer_limits(high=1 << 30)
        await original(writer, response)

    monkeypatch.setattr(server, "write_response", buffered)
    d = start_daemon(_script(fake_qsv, 'sys.stdout.write("x" * (32 << 20))\n'))
    body = json.dumps({"command": "count"}).encode()
    s = socket.create_connection(("127.0.0.1", d.port))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.sendall(
        f"POST /v1/run HTTP/1.1\r\nHost: 127.0.0.1:{d.port}\r\n"
        f"Authorization: Bearer {d.daemon.local_token}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    s.recv(1024)
    time.sleep(1.0)  # the handler is now waiting in wait_closed()
    started = time.monotonic()
    d.stop()
    assert time.monotonic() - started < 10
    s.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_socket_path_too_long_is_a_clear_error(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    long_path = tmp_path / ("x" * 120) / "qsvd.sock"
    daemon = Daemon(
        DaemonConfig(binary=_script(fake_qsv, ECHO)),
        home=tmp_path,
        tcp_port=0,
        socket_path=long_path,
    )
    with pytest.raises(ValueError, match=r"too long for a Unix socket.*QSV_CLIENTD_SOCKET"):
        DaemonThread(daemon).start()
    with pytest.raises(QsvDaemonError, match="QSV_CLIENTD_SOCKET"):
        DaemonClient(socket_path=long_path).status()
    with pytest.raises(QsvDaemonError, match="QSV_CLIENTD_SOCKET"):
        asyncio.run(AsyncDaemonClient(socket_path=long_path).status())


# CSV on stdout like qsv: a quoted field with a line break, and many rows to truncate
TABLE_QSV = """
import csv, io
if sys.argv[1] == "count":
    print(3)
    sys.exit(0)
buf = io.StringIO()
w = csv.writer(buf, lineterminator="\\n")
w.writerow(["field", "note"])
w.writerow(["a", "two\\nlines"])
for i in range(int(os.environ.get("FAKE_ROWS", "1"))):
    w.writerow([f"row{i}", "x" * 50])
sys.stdout.write(buf.getvalue())
"""


def test_query_returns_rows_for_csv_output(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, data: Path
) -> None:
    d = start_daemon(_script(fake_qsv, TABLE_QSV))
    ds_id = _register(d, data)
    token = d.pair(_sha(data))

    def query(command: str, *args: str) -> dict[str, Any]:
        reply = d.http(
            "POST",
            f"/v1/datasets/{ds_id}/query",
            {"command": command, "args": list(args)},
            token=token,
        )
        assert reply.status == 200, reply.body
        out: dict[str, Any] = reply.json()
        return out

    freq = query("frequency", "-s", "name")
    assert freq["headers"] == ["field", "note"]
    assert freq["rows"] == [["a", "two\nlines"], ["row0", "x" * 50]]
    assert freq["stdout"].startswith("field,note\n")  # still there for older pages
    # not CSV: no table
    assert "rows" not in query("frequency", "--json")
    assert "rows" not in query("count")


# Like qsv on real CSV: enough of count, index, slice, search and sort for stored results
CSV_QSV = r"""
import csv, json, re, time
def records(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))
def write(rows):
    w = csv.writer(sys.stdout, lineterminator="\n")
    w.writerows(rows)
def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
cmd, path = sys.argv[1], sys.argv[-1]
if cmd == "count":
    print(max(len(records(path)) - 1, 0))
elif cmd == "index":
    open(path + ".idx", "w").write("")
elif cmd == "slice":
    if os.environ.get("FAKE_SLOW") and "--start" not in sys.argv:  # the query, not a page
        time.sleep(1.5)
    rows = records(path)
    start = int(opt("--start", 0))
    length = opt("--len")
    body = rows[1:][start:] if length is None else rows[1:][start:start + int(length)]
    write(rows[:1] + body)
elif cmd == "search":
    rows = records(path)
    pattern = sys.argv[-2]
    flags = re.IGNORECASE if "--ignore-case" in sys.argv or "-i" in sys.argv else 0
    col = opt("--select")
    idx = rows[0].index(col.strip('"')) if col else None
    hits = [r for r in rows[1:]
            if any(re.search(pattern, v, flags) for v in (r if idx is None else [r[idx]]))]
    if not hits:
        sys.stderr.write(json.dumps({"error": {"kind": "no_match", "message": "no match",
                                               "exit_code": 1, "command": "search"}}) + "\n")
        sys.exit(1)
    write(rows[:1] + hits)
elif cmd == "sort":
    rows = records(path)
    i = int(opt("--select")) - 1
    natural = lambda v: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", v)]
    write(rows[:1] + sorted(rows[1:], key=lambda r: natural(r[i]), reverse="--reverse" in sys.argv))
else:
    sys.exit(2)
"""


def _people(path: Path, n: int) -> Path:
    path.parent.mkdir(exist_ok=True)
    teams = ["red", "blue", "green"]
    lines = ["id,name,team"] + [f"{i},person{i},{teams[i % 3]}" for i in range(1, n + 1)]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_large_query_is_paged_searched_and_ordered(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = _people(tmp_path / "data" / "people.csv", 300)
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000)
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "slice", "args": []}, token=token
    ).json()
    assert reply["truncated"] is True
    result = reply["result"]
    assert result["total"] == 300 and result["headers"] == ["id", "name", "team"]
    assert reply["rows"][0] == ["1", "person1", "blue"] and len(reply["rows"]) == 100

    def page(**body: object) -> dict[str, Any]:
        r = d.http(
            "POST", f"/v1/datasets/{ds_id}/page", {"result": result["id"], **body}, token=token
        )
        assert r.status == 200, r.body
        out: dict[str, Any] = r.json()
        return out

    last = page(start=290, length=20)
    assert [r[0] for r in last["rows"]] == [str(i) for i in range(291, 301)]
    assert (last["total"], last["filtered"]) == (300, 300)
    # search is case-insensitive and across columns; order is natural, on the searched rows
    blue = page(start=0, length=5, search="BLUE", order={"column": 0, "desc": True})
    assert blue["filtered"] == 100
    assert [r[0] for r in blue["rows"]] == ["298", "295", "292", "289", "286"]
    # a value that starts with - is searched for, not read as a flag
    assert page(search="-x")["filtered"] == 0
    assert page(search="person3")["filtered"] == 12  # 3, 30-39, 300
    # one stored result per page, in the daemon's own directory
    results = d.daemon.home / "results"
    assert len(list(results.iterdir())) == 1
    assert sorted(p.name for p in data.parent.iterdir()) == ["people.csv"]


def test_stored_results_are_replaced_capped_and_go_stale(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = _people(tmp_path / "data" / "people.csv", 300)
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000)
    ds_id = _register(d, data)
    token = d.pair(_sha(data))

    def query() -> str:
        reply = d.http(
            "POST", f"/v1/datasets/{ds_id}/query", {"command": "slice", "args": []}, token=token
        )
        assert reply.status == 200, reply.body
        rid: str = reply.json()["result"]["id"]
        return rid

    def page(rid: str) -> int:
        return d.http("POST", f"/v1/datasets/{ds_id}/page", {"result": rid}, token=token).status

    first = query()
    second = query()
    assert page(first) == 410  # replaced by the newer query
    assert page(second) == 200
    # the file changed in place (an edit, or a fetch with --allow-replace): the result is stale
    with data.open("a") as fh:
        fh.write("301,person301,red\n")
    assert page(second) == 409
    # a query that stores nothing still replaces the previous result
    third = query()
    counted = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "count", "args": []}, token=token
    )
    assert counted.status == 200, counted.body
    assert page(third) == 410

    # over browser_max_result: refused, and nothing is left behind
    small = start_daemon(
        _script(fake_qsv, CSV_QSV), browser_max_output=4000, browser_max_result=1000
    )
    small_id = _register(small, data)
    small_token = small.pair(_sha(data))
    big = small.http(
        "POST",
        f"/v1/datasets/{small_id}/query",
        {"command": "slice", "args": []},
        token=small_token,
    )
    assert big.status == 413, big.body
    assert not any((small.daemon.home / "results").rglob("*.csv"))


def test_stored_results_are_removed_with_their_page(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = _people(tmp_path / "data" / "people.csv", 300)
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000)
    ds_id = _register(d, data)
    results = d.daemon.home / "results"

    def store(token: str) -> None:
        reply = d.http(
            "POST", f"/v1/datasets/{ds_id}/query", {"command": "slice", "args": []}, token=token
        )
        assert reply.status == 200, reply.body

    store(d.pair(_sha(data)))
    store(d.pair(_sha(data)))
    assert len(list(results.iterdir())) == 2  # one per paired page
    revoked = d.http("POST", "/v1/tokens/revoke", {}, origin=None, token=d.daemon.local_token)
    assert revoked.status == 200
    assert list(results.iterdir()) == []
    store(d.pair(_sha(data)))
    forgot = d.http("DELETE", f"/v1/datasets/{ds_id}", origin=None, token=d.daemon.local_token)
    assert forgot.status == 200
    assert list(results.iterdir()) == []
    # a result left on disk by an earlier run belongs to no page: cleared at startup
    (results / "orphan").mkdir()
    again = start_daemon(_script(fake_qsv, CSV_QSV))
    assert again.daemon.home == d.daemon.home
    assert not (results / "orphan").exists()


def test_drill_down_pages_past_its_limit(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = _people(tmp_path / "data" / "people.csv", 1800)
    d = start_daemon(_script(fake_qsv, CSV_QSV))
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    reply = d.http(
        "POST",
        f"/v1/datasets/{ds_id}/rows",
        {"column": "team", "value": "red", "limit": 500},
        token=token,
    ).json()
    assert len(reply["rows"]) == 500 and reply["result"]["total"] == 600
    rest = d.http(
        "POST",
        f"/v1/datasets/{ds_id}/page",
        {"result": reply["result"]["id"], "start": 500, "length": 500},
        token=token,
    ).json()
    assert len(rest["rows"]) == 100
    assert all(r[2] == "red" for r in rest["rows"])


def test_concurrent_pages_each_match_their_own_search(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    data = _people(tmp_path / "data" / "people.csv", 300)
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000)
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    rid = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "slice", "args": []}, token=token
    ).json()["result"]["id"]
    searches = ["red", "blue", "green", "person1", "red", ""] * 3

    def page(term: str) -> tuple[str, dict[str, Any]]:
        r = d.http(
            "POST",
            f"/v1/datasets/{ds_id}/page",
            {"result": rid, "search": term, "length": 50},
            token=token,
        )
        assert r.status == 200, r.body
        return term, r.json()

    with ThreadPoolExecutor(6) as pool:
        for term, reply in pool.map(page, searches):
            assert len(reply["rows"]) == min(50, reply["filtered"])
            assert all(term in ",".join(row) for row in reply["rows"])


def test_fetched_copy_keeps_the_file_name_and_is_reported(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    import subprocess

    data = tmp_path / "people.csv"
    data.write_text("name,team\nold,red\n")
    server = _Server(b"name,team\nnew,blue\n")
    try:
        d = start_daemon(_script(fake_qsv, HEADER_QSV))
        ds_id = _register(d, data, source_url=server.url)
        token = d.pair(_sha(data))
        info = d.http("GET", f"/v1/datasets/{ds_id}", token=token).json()
        assert info["fetched"] is False
        assert d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token).status == 200
        cache = d.daemon.home / "cache" / ds_id
        # named like the user's file: viz titles the schematic after it
        assert [p.name for p in cache.iterdir()] == ["people.csv"]
        assert d.http("GET", f"/v1/datasets/{ds_id}", token=token).json()["fetched"] is True

        # a copy fetched before this fix was named data.csv; the next fetch cleans it up
        ds = d.daemon.registry.datasets[ds_id]
        (cache / "people.csv").rename(cache / "data.csv")
        (cache / "data.stats.csv").write_text("")
        ds.path = str(cache / "data.csv")
        assert d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token).status == 200
        assert [p.name for p in cache.iterdir()] == ["people.csv"]

        # approval says which copy is read, and where the request came from
        code = d.http("POST", "/v1/pair", {"digest": ds.digest}).json()["code"]
        proc = subprocess.run(
            [sys.executable, "-m", "qsv_client.daemon", "approve", code, "--yes"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        assert f"the fetched copy of {data.resolve()}" in proc.stdout
        assert "Origin: null" in proc.stdout

        # registering the file again switches back to it
        _register(d, data)
        assert d.http("GET", f"/v1/datasets/{ds_id}", token=token).json()["fetched"] is False
        assert d.daemon.registry.datasets[ds_id].path == str(data.resolve())
    finally:
        server.close()


def test_fetch_cleanup_spares_data_named_like_a_sidecar(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = tmp_path / "data.stats.csv"
    data.write_text("name,team\nold,red\n")
    server = _Server(b"name,team\nnew,blue\n")
    try:
        d = start_daemon(_script(fake_qsv, HEADER_QSV))
        ds_id = _register(d, data, source_url=server.url)
        token = d.pair(_sha(data))
        assert d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token).status == 200
        cache = d.daemon.home / "cache" / ds_id
        # as fetched before the copy kept the file's name: data.csv, whose stats sidecar
        # pattern data.stats.csv* also matches the new copy's name
        ds = d.daemon.registry.datasets[ds_id]
        (cache / "data.stats.csv").rename(cache / "data.csv")
        ds.path = str(cache / "data.csv")
        assert d.http("POST", f"/v1/datasets/{ds_id}/fetch", {}, token=token).status == 200
        assert [p.name for p in cache.iterdir()] == ["data.stats.csv"]
        assert Path(d.daemon.registry.datasets[ds_id].path).is_file()
    finally:
        server.close()


def test_disconnect_withdraws_only_that_page(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = _people(tmp_path / "data" / "people.csv", 300)
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000)
    ds_id = _register(d, data)
    results = d.daemon.home / "results"
    leaving, staying = d.pair(_sha(data)), d.pair(_sha(data))
    for token in (leaving, staying):
        reply = d.http(
            "POST", f"/v1/datasets/{ds_id}/query", {"command": "slice", "args": []}, token=token
        )
        assert reply.status == 200, reply.body
    assert len(list(results.iterdir())) == 2

    bye = d.http("POST", f"/v1/datasets/{ds_id}/disconnect", {}, token=leaving)
    assert bye.status == 200 and bye.json() == {"revoked": 1}
    assert len(list(results.iterdir())) == 1
    assert d.http("GET", f"/v1/datasets/{ds_id}", token=leaving).status == 401
    assert d.http("GET", f"/v1/datasets/{ds_id}", token=staying).status == 200
    # a page can only withdraw itself: an unknown token is refused
    assert d.http("POST", f"/v1/datasets/{ds_id}/disconnect", {}, token=leaving).status == 401
    # the local tier has no token to withdraw
    local = d.http(
        "POST", f"/v1/datasets/{ds_id}/disconnect", {}, origin=None, token=d.daemon.local_token
    )
    assert local.json() == {"revoked": 0}
    assert len(list(results.iterdir())) == 1


def test_first_page_over_the_cap_still_returns_the_result(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = tmp_path / "data" / "wide.csv"
    data.parent.mkdir()
    data.write_text("id,blob\n" + "".join(f"{i},{'x' * 400}\n" for i in range(300)))
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=8000)
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "slice", "args": []}, token=token
    )
    assert reply.status == 200, reply.body
    body = reply.json()
    # 100 rows of 400 bytes don't fit 8000 bytes: no first page, but the result to page
    assert "rows" not in body and body["result"]["total"] == 300
    small = d.http(
        "POST",
        f"/v1/datasets/{ds_id}/page",
        {"result": body["result"]["id"], "length": 10},
        token=token,
    )
    assert small.status == 200 and len(small.json()["rows"]) == 10


def test_no_result_is_kept_for_a_page_gone_while_its_query_ran(
    fake_qsv: Callable[[str], str],
    start_daemon: StartDaemon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("FAKE_SLOW", "1")
    data = _people(tmp_path / "data" / "people.csv", 300)
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000)
    ds_id = _register(d, data)
    results = d.daemon.home / "results"

    def slow_query(token: str, origin: str | None = "null") -> int:
        return d.http(
            "POST",
            f"/v1/datasets/{ds_id}/query",
            {"command": "slice", "args": []},
            origin=origin,
            token=token,
        ).status

    with ThreadPoolExecutor(1) as pool:
        # the page disconnects while its query runs
        token = d.pair(_sha(data))
        running = pool.submit(slow_query, token)
        time.sleep(0.5)
        assert d.http("POST", f"/v1/datasets/{ds_id}/disconnect", {}, token=token).status == 200
        assert running.result() == 410
        assert list(results.iterdir()) == []
        # the dataset is forgotten while a local query runs
        running = pool.submit(slow_query, d.daemon.local_token, None)
        time.sleep(0.5)
        forgot = d.http("DELETE", f"/v1/datasets/{ds_id}", origin=None, token=d.daemon.local_token)
        assert forgot.status == 200
        assert running.result() == 410
        assert list(results.iterdir()) == []


def test_expired_tokens_results_are_swept(
    fake_qsv: Callable[[str], str],
    start_daemon: StartDaemon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsv_client.daemon import server

    data = _people(tmp_path / "data" / "people.csv", 300)
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000, token_ttl=1.0)
    ds_id = _register(d, data)
    results = d.daemon.home / "results"
    token = d.pair(_sha(data))
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/query", {"command": "slice", "args": []}, token=token
    )
    assert reply.status == 200, reply.body
    # any request sweeps, even one whose token has expired
    time.sleep(1.2)
    assert d.http("GET", "/v1/hello").status == 200
    assert list(results.iterdir()) == []

    # and so does the daemon when no request comes
    monkeypatch.setattr(server, "_SWEEP_EVERY", 0.2)
    idle = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000, token_ttl=1.0)
    idle_id = _register(idle, data)
    token = idle.pair(_sha(data))
    reply = idle.http(
        "POST", f"/v1/datasets/{idle_id}/query", {"command": "slice", "args": []}, token=token
    )
    assert reply.status == 200, reply.body
    idle_results = idle.daemon.home / "results"
    assert len(list(idle_results.iterdir())) == 1
    deadline = time.monotonic() + 5
    while list(idle_results.iterdir()):
        assert time.monotonic() < deadline, "the idle sweep never ran"
        time.sleep(0.1)
