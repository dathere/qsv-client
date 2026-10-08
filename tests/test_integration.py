"""Against a real qsv binary ($QSV_BIN or `qsv` on PATH). Assertions that depend on
structured errors adapt to whether the binary supports QSV_ERROR_FORMAT=json."""

from __future__ import annotations

from pathlib import Path

import pytest

from qsv_client import (
    AsyncQsv,
    Qsv,
    QsvCsvError,
    QsvError,
    QsvIOError,
    QsvNoMatch,
    QsvUsageError,
    QsvVersionError,
)


def test_capabilities(qsv: Qsv) -> None:
    caps = qsv.capabilities
    assert caps.binary in {"qsv", "qsvlite", "qsvdp", "qsvmcp"}
    assert caps.version_tuple >= (20, 0, 0)
    assert caps.has_command("count")
    assert caps.has_command("stats")
    assert qsv.version == caps.version


def test_count_headers_stats_frequency(qsv: Qsv, csv_files: Path) -> None:
    ok = csv_files / "ok.csv"
    assert qsv.count(ok) == 3
    assert qsv.headers(ok) == ["name", "n"]
    stats = {row["field"]: row for row in qsv.stats(ok)}
    assert stats["n"]["type"] == "Integer"
    freq = [r for r in qsv.frequency(ok, "--select", "n") if r["field"] == "n"]
    assert {r["value"]: r["count"] for r in freq}["2"] == "2"


def test_stdout_path_streams_to_file(qsv: Qsv, csv_files: Path) -> None:
    out = csv_files / "nested" / "stats.csv"
    res = qsv.run("stats", csv_files / "ok.csv", stdout_path=out)
    assert res.stdout is None
    assert out.read_text().startswith("field,")
    assert {r["field"] for r in res.csv_rows()} == {"name", "n"}


def test_stdin_input(qsv: Qsv) -> None:
    assert qsv.count("-", stdin="a,b\n1,2\n3,4\n") == 2


def test_csv_error(qsv: Qsv, csv_files: Path) -> None:
    with pytest.raises(QsvError) as exc:
        qsv.run("select", "a", csv_files / "ragged.csv")
    assert exc.value.exit_code == 1
    if qsv.capabilities.supports_json_errors:
        assert type(exc.value) is QsvCsvError
        assert exc.value.structured
        assert exc.value.command == "select"
    else:
        assert "found record with 1 fields" in exc.value.message


def test_io_error(qsv: Qsv, csv_files: Path) -> None:
    with pytest.raises(QsvError) as exc:
        qsv.count(csv_files / "missing.csv")
    if qsv.capabilities.supports_json_errors:
        assert type(exc.value) is QsvIOError


def test_usage_error(qsv: Qsv, csv_files: Path) -> None:
    with pytest.raises(QsvUsageError) as exc:
        qsv.run("count", "--bogus", csv_files / "ok.csv")
    assert exc.value.exit_code == 2
    assert "--bogus" in exc.value.message


def test_no_match(qsv: Qsv, csv_files: Path) -> None:
    with pytest.raises(QsvError) as exc:
        qsv.run("search", "zzz", csv_files / "ok.csv")
    assert exc.value.exit_code == 1
    if qsv.capabilities.supports_json_errors:
        assert type(exc.value) is QsvNoMatch
    # check=False hands the outcome back instead
    res = qsv.run("search", "zzz", csv_files / "ok.csv", check=False)
    assert not res.ok


def test_min_version(qsv_bin: str, csv_files: Path) -> None:
    assert Qsv(qsv_bin, min_version="1.0.0").count(csv_files / "ok.csv") == 3
    with pytest.raises(QsvVersionError):
        Qsv(qsv_bin, min_version="999.0.0").count(csv_files / "ok.csv")
    # touching .capabilities first must not let a too-old binary slip through later
    too_old = Qsv(qsv_bin, min_version="999.0.0")
    with pytest.raises(QsvVersionError):
        _ = too_old.capabilities
    with pytest.raises(QsvVersionError):
        too_old.count(csv_files / "ok.csv")


async def test_async_min_version(qsv_bin: str, csv_files: Path) -> None:
    too_old = AsyncQsv(qsv_bin, min_version="999.0.0")
    with pytest.raises(QsvVersionError):
        await too_old.capabilities()
    with pytest.raises(QsvVersionError):
        await too_old.count(csv_files / "ok.csv")


async def test_async_client(qsv_bin: str, csv_files: Path) -> None:
    qsv = AsyncQsv(qsv_bin, timeout=120)
    assert await qsv.count(csv_files / "ok.csv") == 3
    assert (await qsv.capabilities()).has_command("count")
    with pytest.raises(QsvUsageError):
        await qsv.run("count", "--bogus", csv_files / "ok.csv")
