"""qsv-clientd edge cases: HTTP parsing, allowlist, pairing, registry and stored results."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import DaemonThread
from test_daemon import CSV_QSV, _people, _register, _script, _sha

from qsv_client.daemon import DaemonConfig
from qsv_client.daemon import pairing as pairing_module
from qsv_client.daemon._http import HttpError, Request, read_head
from qsv_client.daemon.allowlist import NotAllowed, build_query
from qsv_client.daemon.pairing import Pairing
from qsv_client.daemon.registry import Fingerprint, Registry, SchematicSource
from qsv_client.daemon.server import Daemon

# -- HTTP parsing ---------------------------------------------------------------------------


def _request(body: bytes) -> Request:
    return Request("POST", "/v1/run", {}, {}, body)


def test_request_json_body() -> None:
    assert _request(b"").json() == {}
    assert _request(b'{"a": 1}').json() == {"a": 1}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{not json", "not JSON"),
        (b"\xff\xfe\xfa", "not JSON"),
        (b"[1, 2]", "must be a JSON object"),
    ],
)
def test_request_json_refuses_non_objects(body: bytes, message: str) -> None:
    with pytest.raises(HttpError, match=message) as info:
        _request(body).json()
    assert (info.value.status, info.value.code) == (400, "bad_json")


async def _head(raw: bytes) -> Request:
    reader = asyncio.StreamReader(limit=64 * 1024)
    reader.feed_data(raw)
    reader.feed_eof()
    return await read_head(reader)


@pytest.mark.parametrize(
    ("raw", "status", "message"),
    [
        # over the stream's own buffer limit, before MAX_HEAD is even checked
        (b"GET / HTTP/1.1\r\nX-a: " + b"a" * 70_000 + b"\r\n\r\n", 413, "too large"),
        (b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n", 400, "incomplete"),
        (b"GET /v1/hello\r\n\r\n", 400, "malformed request line"),
        (b"GET  /v1/hello HTTP/1.1\r\n\r\n", 400, "malformed request line"),
        (b"G3T /v1/hello HTTP/1.1\r\n\r\n", 400, "unsupported"),
        (b"GET /v1/hello HTTP/1.1\r\nno colon\r\n\r\n", 400, "malformed header"),
        (b"GET /v1/hello HTTP/1.1\r\n: empty name\r\n\r\n", 400, "malformed header"),
        (b"GET /v1/hello HTTP/1.1\r\nHost : x\r\n\r\n", 400, "malformed header"),
        (b"GET v1/hello HTTP/1.1\r\n\r\n", 400, "must be a path"),
    ],
    # short ids: pytest puts the test id in PYTEST_CURRENT_TEST, and a 70 KB value is over
    # Windows' environment variable limit, which kills the test process outright
    ids=lambda v: v.decode()[:40] if isinstance(v, bytes) else None,
)
async def test_malformed_heads_are_refused(raw: bytes, status: int, message: str) -> None:
    with pytest.raises(HttpError, match=message) as info:
        await _head(raw)
    assert info.value.status == status


async def test_a_closed_connection_is_not_a_bad_request() -> None:
    with pytest.raises(ConnectionResetError):
        await _head(b"")


async def test_head_is_parsed() -> None:
    req = await _head(b"GET /v1/a%20b?x=1&y=2 HTTP/1.0\r\nX-Thing:  v \r\n\r\n")
    assert (req.method, req.path, req.query) == ("GET", "/v1/a b", {"x": "1", "y": "2"})
    assert req.headers == {"x-thing": "v"}


# -- allowlist ------------------------------------------------------------------------------

ALL: frozenset[str] = frozenset()


@pytest.mark.parametrize("args", [["-s"], ["-s", True], ["--limit"]])
def test_option_without_a_value_is_refused(args: list[object]) -> None:
    with pytest.raises(NotAllowed, match="needs a value"):
        build_query("frequency", args, ALL)


@pytest.mark.parametrize("value", [["a"], {"a": 1}])
def test_option_value_must_be_a_string_or_number(value: object) -> None:
    # str() of a list would otherwise reach qsv as "['a']"
    with pytest.raises(NotAllowed, match="strings or numbers"):
        build_query("frequency", ["-s", value], ALL)


def test_negative_numbers_are_values_not_flags() -> None:
    # slice counts a negative start from the end; search's regex may be a negative number
    assert build_query("slice", ["--start", "-5"], ALL) == ["slice", "--start", "-5"]
    assert build_query("slice", ["--start=-5"], ALL) == ["slice", "--start", "-5"]
    assert build_query("search", ["-1.5"], ALL) == ["search", "-1.5"]
    with pytest.raises(NotAllowed):
        build_query("slice", ["--start", "-x"], ALL)


# -- pairing --------------------------------------------------------------------------------


def test_codes_are_unique_among_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    codes = iter(["AAA-AAA", "AAA-AAA", "AAA-AAA", "BBB-BBB"])
    monkeypatch.setattr(pairing_module, "_code", lambda: next(codes))
    p = Pairing(token_ttl=60.0)
    first, second = p.request("ds", "null"), p.request("ds", "null")
    assert first is not None and second is not None
    assert (first.code, second.code) == ("AAA-AAA", "BBB-BBB")
    assert p.find_code("bbb bbb") is second


def test_deny_unknown_code() -> None:
    p = Pairing(token_ttl=60.0)
    assert not p.deny("ZZZ-ZZZ")


# -- registry -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fp",
    [
        None,
        "sha256:abc",
        {"algo": "md5", "digest": "ab", "bytes": 1},
        {"algo": "sha256", "digest": 1, "bytes": 1},
        {"algo": "sha256", "digest": "ab", "bytes": "1"},
    ],
)
def test_schematic_source_ignores_bad_fingerprints(fp: object) -> None:
    src = SchematicSource.from_json({"fingerprint": fp, "viz_args": "--x", "redacted": 3})
    assert src.fingerprint is None
    assert (src.viz_args, src.redacted) == ((), ())


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]"])
def test_schematic_source_from_html_ignores_bad_json(payload: str) -> None:
    html = f'<script type="application/json" id="qsv-schematic-source">{payload}</script>'
    assert SchematicSource.from_html(html) is None


def test_registry_match_and_remove(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "datasets.json")
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("x\n1\n")
    b.write_text("x\n2\n")
    fa, fb = Fingerprint.sha256_of(a), Fingerprint.sha256_of(b)
    src = SchematicSource.from_json({"schematic_uuid": "u-b"})
    ds_a = reg.add(a, fingerprint=fa, viz_args=[])
    ds_b = reg.add(b, fingerprint=fb, viz_args=[], source=src)
    assert reg.match("u-b", None) is ds_b
    assert reg.match("u-b", fa.digest) is ds_b  # the render uuid wins
    assert reg.match("u-unknown", fa.digest.upper()) is ds_a
    assert reg.match("u-unknown", None) is None
    assert reg.match(None, "0" * 64) is None
    assert reg.match(None, None) is None
    assert reg.remove(ds_a.id)
    assert not reg.remove(ds_a.id)
    assert list(Registry(tmp_path / "datasets.json").datasets) == [ds_b.id]


# -- stored results -------------------------------------------------------------------------

StartDaemon = Callable[..., DaemonThread]


@pytest.fixture
def start_daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartDaemon]:
    home = Path(tempfile.mkdtemp(prefix="qcu"))  # short: Unix socket paths are limited
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(home))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    started: list[DaemonThread] = []

    def start(binary: str, **settings: Any) -> DaemonThread:
        thread = DaemonThread(
            Daemon(DaemonConfig(binary=binary, **settings), home=home, tcp_port=0)
        )
        started.append(thread.start())
        return thread

    yield start
    for thread in started:
        thread.stop()
    shutil.rmtree(home, ignore_errors=True)


class _Paged:
    """A dataset with a stored query result, as a paired page sees it."""

    def __init__(self, d: DaemonThread, data: Path) -> None:
        self.d = d
        self.ds_id = _register(d, data)
        self.token = d.pair(_sha(data))
        reply = self.post("query", {"command": "slice", "args": []})
        assert reply["truncated"] is True
        self.result: dict[str, Any] = reply["result"]

    def post(self, action: str, body: dict[str, Any], status: int = 200) -> Any:
        r = self.d.http("POST", f"/v1/datasets/{self.ds_id}/{action}", body, token=self.token)
        assert r.status == status, r.body
        return r.json()

    def page(self, status: int = 200, **body: Any) -> Any:
        return self.post("page", {"result": self.result["id"], **body}, status)


@pytest.fixture
def paged(fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path) -> _Paged:
    d = start_daemon(_script(fake_qsv, CSV_QSV), browser_max_output=4000)
    return _Paged(d, _people(tmp_path / "data" / "people.csv", 300))


def test_page_ordered_without_a_search(paged: _Paged) -> None:
    up = paged.page(length=2, order={"column": 2})
    assert up["filtered"] == 300
    assert [r[2] for r in up["rows"]] == ["blue", "blue"]
    down = paged.page(length=2, order={"column": 2, "desc": True})
    assert [r[2] for r in down["rows"]] == ["red", "red"]
    bad = paged.page(422, order={"column": 3})
    assert "out of range" in bad["daemon_error"]["message"]


def test_page_of_a_deleted_file_is_stale(paged: _Paged, tmp_path: Path) -> None:
    (tmp_path / "data" / "people.csv").unlink()
    assert paged.page(409)["daemon_error"]["code"] == "result_stale"


def test_revoking_sweeps_leftover_result_directories(paged: _Paged) -> None:
    results = paged.d.daemon.home / "results"
    stray = results / "left-behind"
    stray.mkdir()
    revoked = paged.d.http(
        "POST", "/v1/tokens/revoke", {}, origin=None, token=paged.d.daemon.local_token
    )
    assert revoked.status == 200
    assert not results.exists() or list(results.iterdir()) == []


def test_drill_down_without_matches_stores_an_empty_result(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    data = _people(tmp_path / "data" / "people.csv", 30)
    d = start_daemon(_script(fake_qsv, CSV_QSV))
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/rows", {"column": "team", "value": "purple"}, token=token
    )
    assert reply.status == 200, reply.body
    out = reply.json()
    assert (out["headers"], out["rows"], out["result"]["total"]) == ([], [], 0)


def test_count_without_a_number_is_a_bad_output(
    fake_qsv: Callable[[str], str], start_daemon: StartDaemon, tmp_path: Path
) -> None:
    counting = "print(max(len(records(path)) - 1, 0))"
    assert counting in CSV_QSV
    d = start_daemon(_script(fake_qsv, CSV_QSV.replace(counting, 'print("many")')))
    data = _people(tmp_path / "data" / "people.csv", 30)
    ds_id = _register(d, data)
    token = d.pair(_sha(data))
    reply = d.http(
        "POST", f"/v1/datasets/{ds_id}/rows", {"column": "team", "value": "red"}, token=token
    )
    assert reply.status == 502, reply.body
    assert reply.json()["daemon_error"]["code"] == "bad_output"
    assert not any((d.daemon.home / "results").rglob("*.csv"))
