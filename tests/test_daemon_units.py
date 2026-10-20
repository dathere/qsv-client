"""qsv-clientd pieces that need no running daemon: allowlist, pairing, registry, errors, HTTP."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qsv_client import (
    QsvCsvError,
    QsvError,
    QsvNoMatch,
    QsvTimeout,
    QsvUsageError,
    QsvVersionError,
)
from qsv_client.daemon._http import HttpError, read_body, read_head
from qsv_client.daemon.allowlist import NotAllowed, build_query, exact_match_regex
from qsv_client.daemon.config import DaemonConfig
from qsv_client.daemon.pairing import CODE_TTL, MAX_REQUESTS_PER_MINUTE, Pairing
from qsv_client.daemon.registry import Fingerprint, Registry, SchematicSource
from qsv_client.errors import error_from_dict

# -- errors ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls", [QsvError, QsvCsvError, QsvNoMatch, QsvUsageError, QsvTimeout, QsvVersionError]
)
def test_error_round_trips_through_dict(cls: type[QsvError]) -> None:
    err = cls("boom", exit_code=2, command="select", args=["qsv", "select"], stderr="e")
    back = error_from_dict(json.loads(json.dumps(err.to_dict())))
    assert type(back) is cls
    assert back.to_dict() == err.to_dict()


def test_unknown_error_kind_keeps_kind() -> None:
    back = error_from_dict({"kind": "brand_new", "message": "m"})
    assert type(back) is QsvError
    assert back.kind == "brand_new"


# -- allowlist ------------------------------------------------------------------------------

ALL: frozenset[str] = frozenset()  # "every command available" (capabilities not consulted)


def test_query_allows_listed_flags() -> None:
    argv = build_query("frequency", ["-s", "name", "--limit=5", "--asc"], ALL)
    assert argv == ["frequency", "-s", "name", "--limit", "5", "--asc"]


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("sqlp", ["select 1"]),
        ("luau", ["map", "x", "1"]),
        ("describegpt", []),
        ("slice", ["-o", "/tmp/x"]),
        ("slice", ["--output=/tmp/x"]),
        ("stats", ["-sname"]),  # attached short-flag value
        ("select", ["name", "/etc/passwd"]),  # extra positional = another input
        ("count", ["/etc/passwd"]),
        ("search", ["-s", "--output", "x"]),  # option value that is a flag
        ("search", []),  # missing <regex>
        ("frequency", [True]),
    ],
)
def test_query_rejects(command: str, args: list[object]) -> None:
    with pytest.raises(NotAllowed):
        build_query(command, args, ALL)


def test_query_respects_installed_commands() -> None:
    with pytest.raises(NotAllowed):
        build_query("table", [], frozenset({"count"}))


def test_exact_match_regex_escapes_like_rust() -> None:
    assert exact_match_regex("-a.b c") == r"^\-a\.b c$"
    assert not exact_match_regex("-x").startswith("-")


# -- pairing --------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pairing_token_is_one_shot_and_scoped() -> None:
    clock = Clock()
    p = Pairing(token_ttl=60.0, clock=clock)
    req = p.request("ds1", "null")
    assert req is not None
    assert p.poll(req.request_id) == ("pending", None)
    assert p.approve(req.code.replace("-", "").lower()) is not None
    status, token = p.poll(req.request_id)
    assert status == "approved" and token
    assert p.poll(req.request_id) == ("unknown", None)
    grant = p.check(token)
    assert grant is not None and grant.dataset_id == "ds1"
    clock.now += 61
    assert p.check(token) is None


def test_pairing_codes_expire_and_rate_limit() -> None:
    clock = Clock()
    p = Pairing(token_ttl=60.0, clock=clock)
    first = p.request("ds", "null")
    assert first is not None
    for _ in range(MAX_REQUESTS_PER_MINUTE - 1):
        assert p.request("ds", "null") is not None
    assert p.request("ds", "null") is None
    clock.now += max(CODE_TTL, 60.0) + 1
    assert p.approve(first.code) is None
    assert p.request("ds", "null") is not None


def test_denied_pairing_never_yields_a_token() -> None:
    p = Pairing(token_ttl=60.0)
    req = p.request("ds", "null")
    assert req is not None
    assert p.deny(req.code)
    assert p.approve(req.code) is None
    assert p.poll(req.request_id) == ("denied", None)


# -- registry -------------------------------------------------------------------------------


def _schematic_html(sha256: str, size: int) -> str:
    source = {
        "schema": 1,
        "schematic_uuid": "u-1",
        "fingerprint": {"algo": "sha256", "digest": sha256, "bytes": size},
        "viz_args": ["--dict-info"],
        "pid": "https://doi.org/10.1234/abc",
    }
    payload = json.dumps(source).replace("/", "\\/")  # how a script-safe JSON writer escapes
    return f'<html><script type="application/json" id="qsv-schematic-source">{payload}</script>'


def test_schematic_source_from_html(tmp_path: Path) -> None:
    src = SchematicSource.from_html(_schematic_html("ab" * 32, 9))
    assert src is not None
    assert src.schematic_uuid == "u-1"
    assert src.fingerprint == Fingerprint("sha256", "ab" * 32, 9)
    assert src.viz_args == ("--dict-info",)
    assert src.pid == "https://doi.org/10.1234/abc"
    assert SchematicSource.from_html("<html>no source</html>") is None


def test_registry_persists_and_merges(tmp_path: Path) -> None:
    data = tmp_path / "d.csv"
    data.write_text("a\n1\n")
    reg = Registry(tmp_path / "datasets.json")
    fp = Fingerprint.sha256_of(data)
    ds = reg.add(data, fingerprint=fp, viz_args=["--dict-info"], source_url="https://x/d.csv")
    again = reg.add(data, fingerprint=fp, viz_args=[], title="Renamed")
    assert again.id == ds.id
    assert again.viz_args == ["--dict-info"]
    assert again.source_url == "https://x/d.csv"
    assert again.title == "Renamed"
    loaded = Registry(tmp_path / "datasets.json")
    assert loaded.match(None, fp.digest.upper()) == again


def test_registry_loads_sha256_only_entries(tmp_path: Path) -> None:
    path = tmp_path / "datasets.json"
    old = {"id": "d1", "path": "/x.csv", "sha256": "ab" * 32, "size": 3}
    path.write_text(json.dumps({"version": 1, "datasets": [old]}))
    ds = Registry(path).datasets["d1"]
    assert ds.fingerprint == Fingerprint("sha256", "ab" * 32, 3)


def test_registry_refuses_output_flag(tmp_path: Path) -> None:
    data = tmp_path / "d.csv"
    data.write_text("a\n1\n")
    reg = Registry(tmp_path / "datasets.json")
    for bad in (["-o", "x.html"], ["--output=x.html"], ["-ox.html"]):
        with pytest.raises(ValueError):
            reg.add(data, fingerprint=Fingerprint.sha256_of(data), viz_args=bad)


# -- config ---------------------------------------------------------------------------------


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "daemon.json"
    path.write_text('{"port": 1, "prot": 2}')
    with pytest.raises(ValueError, match="prot"):
        DaemonConfig.load(path)
    path.write_text('{"port": 1234}')
    assert DaemonConfig.load(path).port == 1234


# -- HTTP parsing ---------------------------------------------------------------------------


async def _parse(raw: bytes, limit: int = 100) -> None:
    reader = asyncio.StreamReader(limit=64 * 1024)
    reader.feed_data(raw)
    reader.feed_eof()
    request = await read_head(reader)
    await read_body(reader, request, limit)


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"POST /v1/run HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", 501),
        (b"POST /v1/run HTTP/1.1\r\nHost: x\r\n\r\n", 411),
        (b"POST /v1/run HTTP/1.1\r\nContent-Length: -1\r\n\r\n", 400),
        (b"POST /v1/run HTTP/1.1\r\nContent-Length: 1000\r\n\r\n", 413),
        (b"POST /v1/run HTTP/1.1\r\nContent-Length: 5\r\n\r\nab", 400),
        (b"GET http://evil/v1/hello HTTP/1.1\r\n\r\n", 400),
        (b"GET /v1/hello HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n", 400),
        (b"GET /v1/hello HTTP/2\r\n\r\n", 400),
        (b"GET /v1/hello HTTP/1.1\r\nX-" + b"a" * 20000 + b": 1\r\n\r\n", 413),
    ],
)
async def test_malformed_requests_are_refused(raw: bytes, status: int) -> None:
    with pytest.raises(HttpError) as info:
        await _parse(raw)
    assert info.value.status == status


def test_stats_never_writes_cache_files() -> None:
    assert build_query("stats", ["-E"], ALL) == ["stats", "-E", "--cache-threshold", "0"]
    with pytest.raises(NotAllowed):
        build_query("stats", ["--cache-threshold", "1"], ALL)


# -- service definitions --------------------------------------------------------------------


def test_service_pins_the_qsv_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import plistlib
    import stat
    import sys

    from qsv_client import QsvNotFound
    from qsv_client.daemon import service

    if sys.platform == "win32":
        pytest.skip("launchd/systemd")
    qsv = tmp_path / "bin" / "qsv"
    qsv.parent.mkdir()
    qsv.write_text("#!/bin/sh\n")
    qsv.chmod(qsv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("QSV_BIN", raising=False)
    monkeypatch.setenv("PATH", str(qsv.parent))
    env = service.service_environment()
    assert env == {"QSV_BIN": str(qsv), "QSV_CLIENTD_HOME": str(tmp_path / "home")}

    plist = plistlib.loads(service.launchd_plist(["/x/qsv-clientd", "serve"], env).encode())
    assert plist["EnvironmentVariables"]["QSV_BIN"] == str(qsv)
    assert plist["ProgramArguments"] == ["/x/qsv-clientd", "serve"]

    unit = service.systemd_unit(["/x/qsv-clientd", "serve"], {"QSV_BIN": '/a b/q"%sv'})
    assert 'Environment="QSV_BIN=/a b/q\\"%%sv"\n' in unit

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(QsvNotFound):
        service.service_environment()


def test_service_overrides_configured_binary_and_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat
    import subprocess
    import sys

    from qsv_client.daemon import service

    if sys.platform == "win32":
        pytest.skip("launchd/systemd")
    lite = tmp_path / "bin" / "qsvlite"
    lite.parent.mkdir()
    lite.write_text("#!/bin/sh\n")
    lite.chmod(lite.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.json").write_text('{"binary": "qsvlite"}')  # a bare name
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QSV_CLIENTD_HOME", "home")  # relative
    monkeypatch.setenv("PATH", str(lite.parent))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    env = service.service_environment()
    assert env["QSV_CLIENTD_HOME"] == str(tmp_path / "home")
    command = service._command(env["QSV_BIN"])
    assert command[-3:] == ["serve", "--binary", str(lite)]

    # Linux: a reinstall restarts the running unit so it picks up the new settings
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(list(args)))
    service.install()
    assert ["systemctl", "--user", "restart", "qsv-clientd"] in calls
    unit = (tmp_path / ".config/systemd/user/qsv-clientd.service").read_text()
    assert f"--binary {lite}" in unit
