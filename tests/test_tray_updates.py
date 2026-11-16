"""The tray's opt-in qsv update check: its settings file and the GitHub request."""

from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from qsv_client import __version__
from qsv_client.tray import updates


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(tmp_path))
    return tmp_path


def test_settings_default_to_off(home: Path) -> None:
    assert updates.load_settings() == updates.Settings(check_updates=False, notified=None)


def test_settings_are_saved_owner_only(home: Path) -> None:
    settings = updates.Settings(check_updates=True, notified="29.0.0")
    updates.save_settings(settings)
    path = home / "tray.json"
    assert json.loads(path.read_text()) == {"check_qsv_updates": True, "notified_version": "29.0.0"}
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    assert updates.load_settings() == settings


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"check_qsv_updates": "yes", "notified_version": 29}',
        '{"check_qsv_updates": 1}',
    ],
)
def test_unreadable_or_odd_settings_are_the_defaults(home: Path, text: str) -> None:
    (home / "tray.json").write_text(text)
    assert updates.load_settings() == updates.Settings()


def test_a_settings_file_that_cant_be_read_is_the_defaults(home: Path) -> None:
    (home / "tray.json").mkdir()  # reading a directory fails with an OSError
    assert updates.load_settings() == updates.Settings()


class _Releases(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b""
    headers_seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        type(self).headers_seen.append(dict(self.headers))
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Releases]]:
    handler = type("Releases", (_Releases,), {"headers_seen": []})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/repos/dathere/qsv/releases/latest"
    monkeypatch.setattr(updates, "RELEASES_API", url)
    yield handler
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("tag", ["29.0.0", "v29.0.0"])
def test_latest_release_reads_the_tag(api: type[_Releases], tag: str) -> None:
    api.body = json.dumps({"tag_name": tag, "html_url": "https://evil.example/"}).encode()
    assert updates.latest_release() == (29, 0, 0)
    (headers,) = api.headers_seen
    assert headers["User-Agent"] == f"qsv-client/{__version__}"
    assert headers["Accept"] == "application/vnd.github+json"


@pytest.mark.parametrize(
    "body",
    [b"[]", b'{"name": "29.0.0"}', b'{"tag_name": 29}', b'{"tag_name": "latest"}', b"\xff", b"{"],
)
def test_an_unexpected_reply_is_a_value_error(api: type[_Releases], body: bytes) -> None:
    api.body = body
    with pytest.raises(ValueError):
        updates.latest_release()


def test_a_reply_too_large_is_a_value_error(
    api: type[_Releases], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "_MAX_BYTES", 10)
    api.body = b'{"tag_name": "29.0.0"}'
    with pytest.raises(ValueError, match="more than 10 bytes"):
        updates.latest_release()


def test_an_error_status_is_an_os_error(api: type[_Releases]) -> None:
    api.status, api.body = 403, b'{"message": "API rate limit exceeded"}'
    with pytest.raises(OSError, match="403"):
        updates.latest_release()


def test_a_reply_that_isnt_http_is_an_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]

        def answer() -> None:
            conn, _ = server.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b"NOT HTTP AT ALL\r\n\r\n")

        threading.Thread(target=answer, daemon=True).start()
        monkeypatch.setattr(updates, "RELEASES_API", f"http://127.0.0.1:{port}/latest")
        with pytest.raises(OSError):
            updates.latest_release()
