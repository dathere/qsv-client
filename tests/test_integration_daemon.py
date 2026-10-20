"""qsv-clientd against a real qsv (skips without one). Must pass on qsv, qsvlite and qsvdp."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from daemon_helpers import DaemonThread

from qsv_client import Qsv, QsvError
from qsv_client.daemon import DaemonClient, DaemonConfig
from qsv_client.daemon.server import Daemon


@pytest.fixture
def daemon(qsv_bin: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[DaemonThread]:
    home = Path(tempfile.mkdtemp(prefix="qcd"))
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(home))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    thread = DaemonThread(Daemon(DaemonConfig(binary=qsv_bin), home=home, tcp_port=0)).start()
    yield thread
    thread.stop()
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def paired(daemon: DaemonThread, qsv: Qsv, tmp_path: Path) -> tuple[DaemonThread, str, str, Path]:
    missing = [
        c
        for c in ("count", "slice", "search", "stats", "frequency")
        if not qsv.capabilities.has_command(c)
    ]
    if missing:
        pytest.skip(f"this qsv lacks {', '.join(missing)}")
    data = tmp_path / "people.csv"
    data.write_text("name,team\nalpha,red\n-beta,blue\ngamma,red\ndelta,red\n")
    reply = daemon.http(
        "POST",
        "/v1/datasets",
        {"path": str(data), "title": "People"},
        origin=None,
        token=daemon.daemon.local_token,
    )
    ds_id: str = reply.json()["id"]
    # BLAKE3 from qsv's `blake3` command, or SHA-256 when this qsv has none (qsvlite)
    algo = "blake3" if qsv.capabilities.has_command("blake3") else "sha256"
    assert reply.json()["algo"] == algo
    if algo == "sha256":
        assert reply.json()["digest"] == hashlib.sha256(data.read_bytes()).hexdigest()
    token = daemon.pair(reply.json()["digest"])
    return daemon, ds_id, token, data


def test_daemon_run_matches_local_run(daemon: DaemonThread, qsv: Qsv, csv_files: Path) -> None:
    client = DaemonClient(cwd=csv_files)
    assert client.run("count", "ok.csv").stdout == qsv.run("count", csv_files / "ok.csv").stdout
    with pytest.raises(QsvError) as remote:
        client.run("select", "nope", "ok.csv")
    with pytest.raises(QsvError) as local:
        qsv.run("select", "nope", csv_files / "ok.csv")
    assert type(remote.value) is type(local.value)
    assert remote.value.kind == local.value.kind


def test_rows_drill_down(paired: tuple[DaemonThread, str, str, Path]) -> None:
    d, ds_id, token, _ = paired
    page = d.http("POST", f"/v1/datasets/{ds_id}/rows", {"limit": 2, "offset": 1}, token=token)
    assert page.json() == {
        "headers": ["name", "team"],
        "rows": [["-beta", "blue"], ["gamma", "red"]],
        "offset": 1,
    }
    red = d.http(
        "POST",
        f"/v1/datasets/{ds_id}/rows",
        {"column": "team", "value": "red", "limit": 10},
        token=token,
    ).json()
    assert [r[0] for r in red["rows"]] == ["alpha", "gamma", "delta"]
    # a value that starts with '-' is matched, not parsed as a flag
    dash = d.http(
        "POST", f"/v1/datasets/{ds_id}/rows", {"column": "name", "value": "-beta"}, token=token
    ).json()
    assert dash["rows"] == [["-beta", "blue"]]
    none = d.http(
        "POST", f"/v1/datasets/{ds_id}/rows", {"column": "name", "value": "zeta"}, token=token
    ).json()
    assert none["rows"] == []


def test_refresh_sees_changes(paired: tuple[DaemonThread, str, str, Path]) -> None:
    d, ds_id, token, data = paired
    first = d.http("POST", f"/v1/datasets/{ds_id}/refresh", {}, token=token).json()
    assert first["changed"] is False and first["rows"] == 4
    with data.open("a") as fh:
        fh.write("epsilon,blue\n")
    second = d.http("POST", f"/v1/datasets/{ds_id}/refresh", {"stats": True}, token=token).json()
    assert second["changed"] is True and second["rows"] == 5
    assert {row["field"] for row in second["stats"]} == {"name", "team"}


def test_query(paired: tuple[DaemonThread, str, str, Path]) -> None:
    d, ds_id, token, _ = paired
    reply = d.http(
        "POST",
        f"/v1/datasets/{ds_id}/query",
        {"command": "frequency", "args": ["--select", "team"]},
        token=token,
    )
    assert reply.status == 200, reply.body
    assert "red" in reply.json()["stdout"]


def test_render(paired: tuple[DaemonThread, str, str, Path], qsv: Qsv, tmp_path: Path) -> None:
    if not qsv.capabilities.has_command("viz"):
        pytest.skip("this qsv has no viz command")
    d, ds_id, token, data = paired
    schematic = tmp_path / "people.html"
    reply = d.http(
        "POST",
        "/v1/datasets",
        {"path": str(data), "schematic_path": str(schematic)},
        origin=None,
        token=d.daemon.local_token,
    )
    assert reply.status == 200
    page = d.http("POST", f"/v1/datasets/{ds_id}/render", {}, token=token)
    assert page.status == 200, page.body[:500]
    assert page.headers["content-type"].startswith("text/html")
    assert schematic.read_bytes() == page.body
    assert schematic.stat().st_mode & 0o777 == 0o644 or schematic.stat().st_mode & 0o044
