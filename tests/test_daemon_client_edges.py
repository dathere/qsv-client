"""DaemonClient and AsyncDaemonClient edge cases: loopback TCP, odd replies, no daemon.

The two clients are mirrored by hand, so each case runs against both.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import DaemonThread

from qsv_client import Qsv, QsvDaemonError, QsvDaemonUnavailable
from qsv_client.daemon import AsyncDaemonClient, DaemonClient, DaemonConfig
from qsv_client.daemon.client import _configured_socket, _runtime_port, qsv_or_daemon
from qsv_client.daemon.config import default_socket_path
from qsv_client.daemon.server import Daemon

ECHO = """
import json, os
print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))
"""


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty daemon home (short path: macOS limits Unix socket paths to 104 bytes)."""
    path = Path(tempfile.mkdtemp(prefix="qce"))
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(path))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def daemon(home: Path, fake_qsv: Callable[[str], str]) -> Iterator[DaemonThread]:
    thread = DaemonThread(
        Daemon(DaemonConfig(binary=fake_qsv(ECHO)), home=home, tcp_port=0)
    ).start()
    yield thread
    thread.stop()


Client = DaemonClient | AsyncDaemonClient


def _call(client: Client, method: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``method`` on either client, running the coroutine for the async one."""
    result = getattr(client, method)(*args, **kwargs)
    if isinstance(client, AsyncDaemonClient):
        return asyncio.run(result)
    return result


CLIENTS = pytest.mark.parametrize("cls", [DaemonClient, AsyncDaemonClient])


# -- loopback TCP against a real daemon ------------------------------------------------------


@CLIENTS
def test_tcp_with_the_local_token_is_local(cls: type[Client], daemon: DaemonThread) -> None:
    client = cls(port=daemon.port, token=daemon.daemon.local_token, cwd="/")
    assert _call(client, "available")
    status = _call(client, "status")
    assert status["service"] == "qsv-clientd"
    assert status["port"] == daemon.port
    result = _call(client, "run", "count", "x.csv")
    assert json.loads(result.stdout)["argv"] == ["count", "x.csv"]


@CLIENTS
def test_tcp_reads_the_local_token_file(cls: type[Client], daemon: DaemonThread) -> None:
    client = cls(port=daemon.port)
    assert client._target.token == daemon.daemon.local_token
    assert _call(client, "status")["service"] == "qsv-clientd"


@CLIENTS
def test_tcp_with_a_wrong_token_is_a_browser(cls: type[Client], daemon: DaemonThread) -> None:
    client = cls(port=daemon.port, token="not-the-token")
    with pytest.raises(QsvDaemonError) as info:
        _call(client, "status")
    assert info.value.status == 401
    assert info.value.code == "unauthorized"


@CLIENTS
def test_tcp_without_a_token_file_is_unavailable(cls: type[Client], home: Path) -> None:
    with pytest.raises(QsvDaemonUnavailable, match="no token"):
        cls(port=9)


def test_runtime_port_is_the_running_daemons(daemon: DaemonThread) -> None:
    assert _runtime_port() == daemon.port


@pytest.mark.parametrize("content", ["not json", "[8080]", '{"port": "8080"}', '{"pid": 1}'])
def test_runtime_port_ignores_bad_runtime_info(home: Path, content: str) -> None:
    assert _runtime_port() is None  # no file at all
    (home / "run").mkdir()
    (home / "run" / "daemon.json").write_text(content)
    assert _runtime_port() is None


def test_malformed_config_falls_back_to_the_default_socket(home: Path) -> None:
    (home / "daemon.json").write_text("{not json")
    assert _configured_socket() == default_socket_path()


# -- a server that is not a well-behaved qsv-clientd -----------------------------------------


class _Stub:
    """A loopback TCP server answering every connection with ``reply``; ``None``: never."""

    def __init__(self, reply: bytes | None) -> None:
        self.reply = reply
        self.requests: list[bytes] = []
        self._sock = socket.create_server(("127.0.0.1", 0))
        self._sock.settimeout(0.05)
        self.port: int = self._sock.getsockname()[1]
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data and (chunk := conn.recv(65536)):
                data += chunk
            self.requests.append(data)
            if self.reply is None:
                self._held.append(conn)
                continue
            conn.sendall(self.reply)
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._sock.close()
        for conn in self._held:
            conn.close()


@pytest.fixture
def stub() -> Iterator[Callable[[bytes | None], _Stub]]:
    made: list[_Stub] = []

    def make(reply: bytes | None) -> _Stub:
        made.append(_Stub(reply))
        return made[-1]

    yield make
    for s in made:
        s.close()


def _http(status: str, body: bytes) -> bytes:
    return f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body


@CLIENTS
def test_tcp_requests_carry_host_and_token(
    cls: type[Client], stub: Callable[[bytes | None], _Stub]
) -> None:
    s = stub(_http("200 OK", b'{"service": "qsv-clientd"}'))
    assert _call(cls(port=s.port, token="t0k"), "available")
    head = s.requests[0].decode("latin-1")
    assert f"Host: 127.0.0.1:{s.port}\r\n" in head
    assert "Authorization: Bearer t0k\r\n" in head


@CLIENTS
def test_another_service_is_not_available(
    cls: type[Client], stub: Callable[[bytes | None], _Stub]
) -> None:
    s = stub(_http("200 OK", b'{"service": "something-else"}'))
    assert not _call(cls(port=s.port, token="t"), "available")


@CLIENTS
def test_invalid_json_reply(cls: type[Client], stub: Callable[[bytes | None], _Stub]) -> None:
    s = stub(_http("200 OK", b"<html>hi</html>"))
    with pytest.raises(QsvDaemonError, match="invalid JSON") as info:
        _call(cls(port=s.port, token="t"), "request", "GET", "/v1/hello")
    assert info.value.status == 200


@CLIENTS
def test_http_error_without_daemon_error(
    cls: type[Client], stub: Callable[[bytes | None], _Stub]
) -> None:
    s = stub(_http("500 Internal Server Error", b"{}"))
    with pytest.raises(QsvDaemonError, match="HTTP 500") as info:
        _call(cls(port=s.port, token="t"), "request", "GET", "/v1/hello")
    assert info.value.status == 500


@CLIENTS
def test_empty_body_decodes_to_an_empty_object(
    cls: type[Client], stub: Callable[[bytes | None], _Stub]
) -> None:
    s = stub(_http("200 OK", b""))
    assert _call(cls(port=s.port, token="t"), "request", "GET", "/v1/hello") == {}


@CLIENTS
def test_a_daemon_that_never_answers_times_out(
    cls: type[Client], stub: Callable[[bytes | None], _Stub]
) -> None:
    s = stub(None)
    with pytest.raises(QsvDaemonError) as info:
        _call(cls(port=s.port, token="t"), "request", "GET", "/v1/hello", wait=0.3)
    assert not isinstance(info.value, QsvDaemonUnavailable)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{}", id="short-body"),
        pytest.param(b"HTTP/1.1 OK\r\n\r\n", id="bad-status-line"),
    ],
)
@pytest.mark.parametrize(
    "cls",
    [
        pytest.param(
            DaemonClient,
            marks=pytest.mark.xfail(
                strict=True,
                reason="sync client lets http.client.HTTPException escape (not an OSError)",
            ),
        ),
        AsyncDaemonClient,
    ],
)
def test_a_garbled_reply_is_a_daemon_error(
    cls: type[Client], reply: bytes, stub: Callable[[bytes | None], _Stub]
) -> None:
    s = stub(reply)
    with pytest.raises(QsvDaemonError, match="talking to qsv-clientd failed"):
        _call(cls(port=s.port, token="t"), "request", "GET", "/v1/hello")


@CLIENTS
def test_connection_refused_is_unavailable(cls: type[Client]) -> None:
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]  # closed again before the client connects
    client = cls(port=port, token="t")
    with pytest.raises(QsvDaemonUnavailable, match=f"127.0.0.1:{port}"):
        _call(client, "request", "GET", "/v1/hello")
    assert not _call(client, "available")


# -- no daemon ---------------------------------------------------------------------------------


def test_async_no_daemon(home: Path) -> None:
    client = AsyncDaemonClient(socket_path=home / "missing.sock")
    with pytest.raises(QsvDaemonUnavailable, match=r"missing\.sock"):
        asyncio.run(client.run("count"))
    assert not asyncio.run(client.available())


def test_qsv_or_daemon_without_a_daemon_runs_locally(home: Path) -> None:
    # POSIX: the socket is missing; Windows: so is the local token
    found = qsv_or_daemon(cwd=str(home))
    assert isinstance(found, Qsv)
    assert found.cwd == str(home)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_socket_path_too_long_is_explained(home: Path) -> None:
    long_path = home / ("x" * 200) / "qsvd.sock"
    for cls in (DaemonClient, AsyncDaemonClient):
        with pytest.raises(QsvDaemonError, match="too long for a Unix socket") as info:
            _call(cls(socket_path=long_path), "request", "GET", "/v1/hello")
        assert not isinstance(info.value, QsvDaemonUnavailable)
