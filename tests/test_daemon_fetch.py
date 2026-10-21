"""fetch.py without the HTTP tier: sources, DOI resolution, downloads, and the qsv `get` path.

Nothing leaves the machine: data comes from a local ``http.server``, and requests to doi.org
are redirected to it by a patched ``urllib.request.urlopen``.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import http.server
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import DaemonThread

from qsv_client.daemon import fetch
from qsv_client.daemon._http import HttpError
from qsv_client.daemon.config import DaemonConfig
from qsv_client.daemon.fetch import FetchError
from qsv_client.daemon.registry import Dataset, Fingerprint
from qsv_client.daemon.server import Daemon

Route = tuple[int, str, bytes]  # status, Content-Type, body


class _Routes:
    """A loopback HTTP server answering each path from a table set by the test."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        routes = self.routes

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, ctype, body = routes.get(self.path, (404, "text/plain", b"not found"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def web() -> Iterator[_Routes]:
    server = _Routes()
    yield server
    server.close()


@pytest.fixture
def doi_org(web: _Routes, monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    """Send requests for https://doi.org/<path> to ``web``'s ``/doi/<path>``; return them."""
    real = urllib.request.urlopen
    seen: list[urllib.request.Request] = []

    def urlopen(req: urllib.request.Request, timeout: float | None = None) -> Any:
        parts = urllib.parse.urlsplit(req.full_url)
        if parts.hostname == "doi.org":
            seen.append(req)
            req = urllib.request.Request(web.url("/doi" + parts.path), headers=req.headers)
        return real(req, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


# -- the daemon side: an unstarted Daemon with a fake qsv -------------------------------------

# answers `slice --len 0 <path>` with the file's first line, and everything else with "1"
PLAIN_QSV = """
if sys.argv[1] == "slice":
    print(open(sys.argv[-1]).readline().rstrip("\\n"))
else:
    print("1")
"""

# a qsv with `get`: logs its argv, and `get cache-fetch ... --output <path>` writes the data
GET_QSV = """
import json
if sys.argv[1:] == ["--capabilities"]:
    print(json.dumps({"binary": "qsv", "version": "26.0.0", "error_formats": ["text", "json"],
                      "commands": ["get", "count", "slice"]}))
    sys.exit(0)
with open(LOG, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["get", "cache-fetch"]:
    open(sys.argv[sys.argv.index("--output") + 1], "w").write("name\\nfetched\\n")
elif sys.argv[1] == "slice":
    print(open(sys.argv[-1]).readline().rstrip("\\n"))
else:
    print("1")
"""


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="qcd"))
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(path))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    yield path
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _daemon(binary: str, home: Path) -> Daemon:
    return Daemon(DaemonConfig(binary=binary), home=home, tcp_port=0)


def _dataset(daemon: Daemon, path: Path, **extra: Any) -> Dataset:
    return daemon.registry.add(path, fingerprint=Fingerprint.sha256_of(path), viz_args=[], **extra)


def _staging_files(daemon: Daemon, ds: Dataset) -> list[str]:
    cache = daemon.home / "cache" / ds.id
    return [p.name for p in cache.iterdir() if p.name.startswith(".qsv-clientd")]


async def test_fetch_needs_a_source(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path
) -> None:
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)
    ds = _dataset(daemon, data)
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (409, "no_source")


@pytest.mark.parametrize(
    "source", ["file:///etc/passwd", "ftp://example.invalid/d.csv", "data:text/csv,a%0A1"]
)
async def test_fetch_refuses_other_schemes(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, source: str
) -> None:
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)
    ds = _dataset(daemon, data, source_url=source)
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (422, "bad_source")
    assert data.read_text() == "name\nold\n"
    assert _staging_files(daemon, ds) == []


@pytest.mark.parametrize("scheme", ["ckan", "dathere", "s3", "gs", "az"])
async def test_qsv_get_schemes_need_qsv_get(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, scheme: str
) -> None:
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)  # qsv 25: no `get`
    ds = _dataset(daemon, data, source_url=f"{scheme}://bucket/d.csv")
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (501, "needs_qsv_get")
    assert _staging_files(daemon, ds) == []


@pytest.mark.parametrize("source", ["s3://bucket/d.csv", "HTTPS://example.invalid/d.csv"])
async def test_fetch_goes_through_qsv_get_when_available(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, source: str
) -> None:
    log = tmp_path / "argv.log"
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(f"LOG = {str(log)!r}\n" + GET_QSV), home)
    ds = _dataset(daemon, data, source_url=source, allow_replace=True)
    result = await fetch.fetch_latest(daemon, ds)
    assert result["via"] == "url" and result["changed"] is True
    assert data.read_text() == "name\nfetched\n"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    name = f"qsv-clientd-{ds.id}.csv"
    assert calls[0] == ["get", source, "--name", name]
    assert calls[1][:4] == ["get", "cache-fetch", name, "--output"]
    staged = Path(calls[1][4])
    assert staged.parent == daemon.home / "cache" / ds.id and not staged.exists()
    assert calls[2] == ["count", str(staged)]
    assert _staging_files(daemon, ds) == []


@pytest.mark.parametrize("ctype", ["text/html", "text/html; charset=utf-8"])
async def test_download_refuses_a_web_page(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, web: _Routes, ctype: str
) -> None:
    web.routes["/d.csv"] = (200, ctype, b"<html>sign in</html>")
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)
    ds = _dataset(daemon, data, source_url=web.url("/d.csv"), allow_replace=True)
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (422, "not_data")
    assert data.read_text() == "name\nold\n"
    assert _staging_files(daemon, ds) == []


async def test_download_failures_keep_the_data(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, web: _Routes
) -> None:
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)
    # a 404 (urllib's HTTPError is an OSError), then a port nobody listens on
    closed = _Routes()
    closed.close()
    for url in (web.url("/missing.csv"), closed.url("/d.csv")):
        ds = _dataset(daemon, data, source_url=url, allow_replace=True)
        with pytest.raises(FetchError) as info:
            await fetch.fetch_latest(daemon, ds)
        assert (info.value.status, info.value.code) == (502, "download_failed")
        assert str(info.value).startswith("download failed: ")
        assert data.read_text() == "name\nold\n"
        assert _staging_files(daemon, ds) == []


async def test_fetch_from_a_doi(
    fake_qsv: Callable[[str], str],
    home: Path,
    tmp_path: Path,
    web: _Routes,
    doi_org: list[urllib.request.Request],
) -> None:
    web.routes["/data/latest.csv"] = (200, "text/csv", b"name\nnew\n")
    web.routes["/doi/10.5072/abc"] = (
        200,
        "application/vnd.datacite.datacite+json",
        json.dumps({"contentUrl": web.url("/data/latest.csv")}).encode(),
    )
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)
    ds = _dataset(daemon, data, pid="doi:10.5072/abc")
    result = await fetch.fetch_latest(daemon, ds)
    assert result["via"] == "doi" and result["changed"] is True
    # without --allow-replace the user's file is untouched and the dataset reads a copy
    assert data.read_text() == "name\nold\n"
    assert Path(ds.path).read_text() == "name\nnew\n"
    assert doi_org[0].get_header("Accept") == "application/vnd.datacite.datacite+json"
    assert doi_org[0].get_header("User-agent") == "qsv-clientd"


async def test_a_doi_without_data_is_refused(
    fake_qsv: Callable[[str], str],
    home: Path,
    tmp_path: Path,
    web: _Routes,
    doi_org: list[urllib.request.Request],
) -> None:
    web.routes["/doi/10.5072/landing"] = (200, "application/json", b'{"url": "https://x"}')
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)
    ds = _dataset(daemon, data, pid="https://doi.org/10.5072/landing")
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (422, "pid_not_data")
    assert "10.5072/landing" in str(info.value) and "--source-url" in str(info.value)
    assert len(doi_org) == 1
    assert _staging_files(daemon, ds) == []


# -- helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "doi"),
    [
        ("doi:10.5072/abc", "10.5072/abc"),
        ("DOI:10.5072/abc", "10.5072/abc"),
        ("  10.5072/abc  ", "10.5072/abc"),
        ("https://doi.org/10.5072/abc", "10.5072/abc"),
        ("http://dx.doi.org/10.5072/a%20b", "10.5072/a b"),
        ("10.5072", None),  # no suffix
        ("https://example.org/10.5072/abc", None),
        ("https://doi.org/about", None),
        ("https://example.org/data.csv", None),
    ],
)
def test_doi_forms(source: str, doi: str | None) -> None:
    assert fetch._doi(source) == doi


def test_datacite_request_quotes_the_doi(
    web: _Routes, doi_org: list[urllib.request.Request]
) -> None:
    assert fetch._datacite_content_url("10.5072/a b?c") is None  # 404 from the local server
    assert urllib.parse.urlsplit(doi_org[0].full_url).path == "/10.5072/a%20b%3Fc"


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        # contentUrl as a string or a list; only http(s) URLs are taken, whatever the case
        ((200, "application/json", b'{"contentUrl": "https://h/d.csv"}'), "https://h/d.csv"),
        (
            (
                200,
                "application/vnd.datacite.datacite+json",
                json.dumps(
                    {"contentUrl": ["file:///etc/passwd", "s3://b/k", 7, "HTTP://h/d.csv"]}
                ).encode(),
            ),
            "HTTP://h/d.csv",
        ),
        ((200, "application/json", b'{"contentUrl": ["file:///etc/passwd"]}'), None),
        ((200, "application/json", b'{"contentUrl": {"url": "https://h"}}'), None),
        ((200, "application/json", b'["https://h/d.csv"]'), None),
        ((200, "application/json", b"{not json"), None),
        ((200, "text/html", b'{"contentUrl": "https://h/d.csv"}'), None),  # a landing page
        ((404, "application/json", b'{"contentUrl": "https://h/d.csv"}'), None),
    ],
)
def test_datacite_content_url(
    web: _Routes, doi_org: list[urllib.request.Request], route: Route, expected: str | None
) -> None:
    web.routes["/doi/10.5072/x"] = route
    assert fetch._datacite_content_url("10.5072/x") == expected


def test_replace_creates_a_missing_target_privately(tmp_path: Path) -> None:
    src = tmp_path / "incoming.csv"
    src.write_text("name\nnew\n")
    target = tmp_path / "gone.csv"
    fetch._replace_keeping_mode(src, target)
    assert target.read_text() == "name\nnew\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("source", "suffix"),
    [
        ("https://h/d.TSV", ".tsv"),
        ("https://h/d.tab?x=.csv", ".tab"),
        ("s3://b/d.parquet", ".csv"),
        ("doi:10.5072/abc", ".csv"),
    ],
)
def test_suffix_follows_the_url_path(source: str, suffix: str) -> None:
    assert fetch._suffix(source) == suffix


# -- download limits: fetch_max_bytes and fetch_timeout ----------------------------------------


class _Stream:
    """A loopback server sending ``chunks`` of a CSV, ``gap`` seconds apart.

    With ``declare`` set it sends that as Content-Length; otherwise none, so only the bytes
    that arrive can show the size (HTTP/1.0: the body ends when the connection closes).
    """

    def __init__(self, chunks: list[bytes], *, gap: float = 0.0, declare: int | None = None):
        self.hits = 0
        stream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stream.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                if declare is not None:
                    self.send_header("Content-Length", str(declare))
                self.end_headers()
                try:
                    for chunk in chunks:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        time.sleep(gap)
                except OSError:
                    pass  # the client gave up, as it should

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/d.csv"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stream() -> Iterator[Callable[..., _Stream]]:
    servers: list[_Stream] = []

    def start(chunks: list[bytes], **kwargs: Any) -> _Stream:
        servers.append(_Stream(chunks, **kwargs))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def _limited(binary: str, home: Path, **limits: Any) -> Daemon:
    return Daemon(DaemonConfig(binary=binary, **limits), home=home, tcp_port=0)


BIG = [b"name\n"] + [b"x" * 99 + b"\n"] * 20  # 2005 bytes


@pytest.mark.parametrize("declare", [len(b"".join(BIG)), None], ids=["declared", "streamed"])
async def test_a_download_over_fetch_max_bytes_is_refused(
    fake_qsv: Callable[[str], str],
    home: Path,
    tmp_path: Path,
    stream: Callable[..., _Stream],
    declare: int | None,
) -> None:
    server = stream(BIG, declare=declare)
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _limited(fake_qsv(PLAIN_QSV), home, fetch_max_bytes=1000)
    ds = _dataset(daemon, data, source_url=server.url, allow_replace=True)
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (413, "too_large")
    assert "1000 bytes (fetch_max_bytes)" in str(info.value)
    assert data.read_text() == "name\nold\n"
    assert _staging_files(daemon, ds) == []


async def test_a_download_of_exactly_fetch_max_bytes_is_used(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, stream: Callable[..., _Stream]
) -> None:
    body = b"name\nnew\n"
    server = stream([body])
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _limited(fake_qsv(PLAIN_QSV), home, fetch_max_bytes=len(body))
    ds = _dataset(daemon, data, source_url=server.url, allow_replace=True)
    assert (await fetch.fetch_latest(daemon, ds))["changed"] is True
    assert data.read_bytes() == body


async def test_a_trickling_download_stops_at_fetch_timeout(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, stream: Callable[..., _Stream]
) -> None:
    # a byte every 0.1s: each read succeeds well inside the socket timeout, so only the
    # overall deadline can stop it
    server = stream([b"name\n"] + [b"x"] * 300, gap=0.1)
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _limited(fake_qsv(PLAIN_QSV), home, fetch_timeout=0.5)
    ds = _dataset(daemon, data, source_url=server.url, allow_replace=True)
    started = time.monotonic()
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert time.monotonic() - started < 10
    assert (info.value.status, info.value.code) == (504, "download_timeout")
    assert "0.5s (fetch_timeout)" in str(info.value)
    assert data.read_text() == "name\nold\n"
    assert _staging_files(daemon, ds) == []


async def test_qsv_get_download_over_fetch_max_bytes_is_refused(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path
) -> None:
    log = tmp_path / "argv.log"
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    # GET_QSV's cache-fetch writes "name\nfetched\n" (13 bytes)
    daemon = _limited(fake_qsv(f"LOG = {str(log)!r}\n" + GET_QSV), home, fetch_max_bytes=12)
    ds = _dataset(daemon, data, source_url="https://example.org/d.csv", allow_replace=True)
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (413, "too_large")
    assert data.read_text() == "name\nold\n"
    assert _staging_files(daemon, ds) == []
    # refused before qsv ever counted it
    assert [json.loads(line)[0] for line in log.read_text().splitlines()] == ["get", "get"]


async def test_qsv_get_runs_are_bounded_by_fetch_timeout(
    fake_qsv: Callable[[str], str],
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "argv.log"
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _limited(fake_qsv(f"LOG = {str(log)!r}\n" + GET_QSV), home, fetch_timeout=42.0)
    real = daemon._browser_run
    timeouts: dict[str, float | None] = {}

    async def spy(ds: Dataset, args: list[str], **kwargs: Any) -> Any:
        if args[0] == "get":
            timeouts[args[1]] = kwargs.get("timeout")
        return await real(ds, args, **kwargs)

    monkeypatch.setattr(daemon, "_browser_run", spy)
    ds = _dataset(daemon, data, source_url="https://example.org/d.csv", allow_replace=True)
    await fetch.fetch_latest(daemon, ds)
    assert timeouts == {"https://example.org/d.csv": 42.0, "cache-fetch": 42.0}


class _Raw:
    """A loopback server that writes ``chunks`` of raw bytes, ``gap`` seconds apart."""

    def __init__(self, chunks: list[bytes], gap: float) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/d.csv"

        def serve() -> None:
            with contextlib.suppress(OSError):
                conn, _ = self.sock.accept()
                with conn:
                    conn.recv(65536)
                    for chunk in chunks:
                        conn.sendall(chunk)
                        time.sleep(gap)

        threading.Thread(target=serve, daemon=True).start()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def raw() -> Iterator[Callable[[list[bytes], float], _Raw]]:
    servers: list[_Raw] = []

    def start(chunks: list[bytes], gap: float) -> _Raw:
        servers.append(_Raw(chunks, gap))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


@pytest.mark.parametrize(
    "chunks",
    [
        # headers that never end: only the watchdog can stop urlopen here
        [b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nX-Pad: "] + [b"a"] * 300,
        # a body that stops arriving: the socket times out after the deadline
        [b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\n\r\nname\n"] + [b""] * 300,
        # a chunk-size line that never ends: read1 blocks inside the body, after urllib has
        # let go of the connection's socket
        [
            b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nname\n\r\n"
        ]
        + [b"0"] * 300,
    ],
    ids=["trickled-headers", "stalled-body", "trickled-chunk-size"],
)
async def test_any_slow_download_is_a_download_timeout(
    fake_qsv: Callable[[str], str],
    home: Path,
    tmp_path: Path,
    raw: Callable[[list[bytes], float], _Raw],
    chunks: list[bytes],
) -> None:
    server = raw(chunks, 0.1)
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _limited(fake_qsv(PLAIN_QSV), home, fetch_timeout=0.5)
    ds = _dataset(daemon, data, source_url=server.url, allow_replace=True)
    started = time.monotonic()
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert time.monotonic() - started < 10
    assert (info.value.status, info.value.code) == (504, "download_timeout")
    assert data.read_text() == "name\nold\n"
    assert _staging_files(daemon, ds) == []


@pytest.mark.parametrize(("timeout", "code"), [(0.5, "download_timeout"), (120, "download_failed")])
def test_a_socket_timeout_before_the_clock_shows_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timeout: float, code: str
) -> None:
    """A socket timeout of the whole fetch_timeout is the deadline, even when the clock and
    the watchdog lag (Windows); a shorter idle timeout (the 60s socket cap) is a failure."""

    class Opener:
        def open(self, req: urllib.request.Request, timeout: float) -> Any:
            raise TimeoutError("timed out")  # at once: before the deadline and the watchdog

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    with pytest.raises(FetchError) as info:
        fetch._download(
            "http://127.0.0.1:9/d.csv",
            tmp_path / "d.csv",
            max_bytes=100,
            timeout=timeout,
            downloads=fetch.Downloads(),
        )
    assert info.value.code == code


async def test_a_malformed_response_is_a_download_failure(
    fake_qsv: Callable[[str], str],
    home: Path,
    tmp_path: Path,
    raw: Callable[[list[bytes], float], _Raw],
) -> None:
    # http.client raises BadStatusLine, an HTTPException, not an OSError
    server = raw([b"NOT HTTP\r\n\r\n"], 0)
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _daemon(fake_qsv(PLAIN_QSV), home)
    ds = _dataset(daemon, data, source_url=server.url, allow_replace=True)
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert (info.value.status, info.value.code) == (502, "download_failed")
    assert data.read_text() == "name\nold\n"


async def test_a_qsv_get_past_fetch_timeout_is_a_download_timeout(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path
) -> None:
    log = tmp_path / "argv.log"
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    slow_get = GET_QSV.replace(
        'if sys.argv[1:3] == ["get", "cache-fetch"]:',
        'if sys.argv[1] == "get" and sys.argv[2] != "cache-fetch":\n'
        "    import time; time.sleep(30)\n"
        'elif sys.argv[1:3] == ["get", "cache-fetch"]:',
    )
    daemon = _limited(fake_qsv(f"LOG = {str(log)!r}\n" + slow_get), home, fetch_timeout=0.5)
    ds = _dataset(daemon, data, source_url="https://example.org/d.csv", allow_replace=True)
    started = time.monotonic()
    with pytest.raises(FetchError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert time.monotonic() - started < 20
    assert (info.value.status, info.value.code) == (504, "download_timeout")
    assert data.read_text() == "name\nold\n"


async def test_a_failed_qsv_get_is_reported_as_it_failed(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path
) -> None:
    log = tmp_path / "argv.log"
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    failing_get = GET_QSV.replace(
        'if sys.argv[1:3] == ["get", "cache-fetch"]:',
        'if sys.argv[1] == "get" and sys.argv[2] != "cache-fetch":\n'
        '    sys.stderr.write("get: 403 Forbidden\\n"); sys.exit(1)\n'
        'elif sys.argv[1:3] == ["get", "cache-fetch"]:',
    )
    daemon = _daemon(fake_qsv(f"LOG = {str(log)!r}\n" + failing_get), home)
    ds = _dataset(daemon, data, source_url="https://example.org/d.csv", allow_replace=True)
    # not a timeout, so not turned into one
    with pytest.raises(HttpError) as info:
        await fetch.fetch_latest(daemon, ds)
    assert info.value.status == 502 and info.value.code != "download_timeout"
    assert "403 Forbidden" in str(info.value)
    assert data.read_text() == "name\nold\n"


def test_the_watchdog_shuts_a_socket_connected_after_it_fired() -> None:
    watchdog = fetch._Watchdog(0)
    deadline = time.monotonic() + 5
    while not watchdog.fired:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with socket.create_server(("127.0.0.1", 0)) as server:
        conn = watchdog.watch(http.client.HTTPConnection)(
            "127.0.0.1", server.getsockname()[1], timeout=5
        )
        conn.connect()
        peer, _ = server.accept()
        with peer:
            # shutting our side down sends the peer an end of data; checked from the peer,
            # since Windows lets a recv on the shut socket itself block until its timeout
            peer.settimeout(2)
            try:
                got = peer.recv(1)
            except TimeoutError:
                pytest.fail("the socket was not shut")
            assert got == b""
        conn.close()


def test_https_connections_go_through_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def do_open(self: Any, http_class: Any, req: Any, **kwargs: Any) -> Any:
        seen["kwargs"] = kwargs
        seen["conn"] = http_class(req.host, **kwargs)
        raise urllib.error.URLError("stop before connecting")

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", do_open)
    watchdog = fetch._Watchdog(60)
    try:
        with pytest.raises(urllib.error.URLError):
            fetch._WatchedHTTPS(watchdog).https_open(
                urllib.request.Request("https://example.org/d.csv")
            )
    finally:
        watchdog.cancel()
    # HTTPSHandler's own TLS arguments got through, and the connection is watched
    assert isinstance(seen["conn"], http.client.HTTPSConnection)
    assert "context" in seen["kwargs"]
    assert "connect" in vars(seen["conn"])


async def test_closing_the_daemon_cuts_off_a_download(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, stream: Callable[..., _Stream]
) -> None:
    # a byte every 0.1s for 30s, with all the time in the world: only close() can stop it
    server = stream([b"name\n"] + [b"x"] * 300, gap=0.1)
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    daemon = _limited(fake_qsv(PLAIN_QSV), home, fetch_timeout=600)
    ds = _dataset(daemon, data, source_url=server.url, allow_replace=True)
    task = asyncio.create_task(fetch.fetch_latest(daemon, ds))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    await daemon.close()
    with pytest.raises(FetchError) as info:
        await asyncio.wait_for(task, 10)
    assert time.monotonic() - started < 5
    assert (info.value.status, info.value.code) == (503, "shutting_down")
    assert data.read_text() == "name\nold\n"
    assert _staging_files(daemon, ds) == []
    # and one that starts once the daemon is closing never connects
    hits = server.hits
    started = time.monotonic()
    with pytest.raises(FetchError, match="shutting down"):
        await asyncio.wait_for(fetch.fetch_latest(daemon, ds), 10)
    assert server.hits == hits and time.monotonic() - started < 5


def test_stopping_a_running_daemon_does_not_wait_for_a_fetch(
    fake_qsv: Callable[[str], str], home: Path, tmp_path: Path, stream: Callable[..., _Stream]
) -> None:
    server = stream([b"name\n"] + [b"x"] * 300, gap=0.1)
    data = tmp_path / "d.csv"
    data.write_text("name\nold\n")
    d = DaemonThread(_limited(fake_qsv(PLAIN_QSV), home, fetch_timeout=600)).start()
    try:
        token = d.daemon.local_token
        body = {"path": str(data), "source_url": server.url, "allow_replace": True}
        ds = d.http("POST", "/v1/datasets", body, origin=None, token=token).json()
        replies: list[int] = []

        def fetch_it() -> None:
            with contextlib.suppress(OSError, http.client.HTTPException):
                path = f"/v1/datasets/{ds['id']}/fetch"
                replies.append(d.http("POST", path, {}, origin=None, token=token).status)

        thread = threading.Thread(target=fetch_it, daemon=True)
        thread.start()
        time.sleep(0.5)
    finally:
        started = time.monotonic()
        d.stop()
    assert time.monotonic() - started < 10  # not the 30s the download would take
    assert data.read_text() == "name\nold\n"
