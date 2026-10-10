"""Client-level unit tests: binary discovery, and the JSON error / --capabilities paths that
only qsv releases after 24.0.0 take (emulated with fake_qsv, in the shapes qsv's own
tests/test_error_format.rs pins)."""

from __future__ import annotations

import importlib.metadata
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import qsv_client
from qsv_client import (
    AsyncQsv,
    Qsv,
    QsvCsvError,
    QsvError,
    QsvIOError,
    QsvNoMatch,
    QsvNotFound,
    QsvUsageError,
    QsvVersionError,
    _process,
    find_qsv,
)


def test_version_matches_package_metadata() -> None:
    assert qsv_client.__version__ == importlib.metadata.version("qsv-client")


# -- find_qsv -------------------------------------------------------------------------------


def _executable(directory: Path, name: str) -> Path:
    """An executable file ``shutil.which`` will find (never run)."""
    path = directory / (name + ".cmd" if sys.platform == "win32" else name)
    path.write_text("")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def bindir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory that is the whole PATH, with QSV_BIN unset."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.delenv("QSV_BIN", raising=False)
    return d


def test_find_qsv_prefers_default_binaries_in_order(bindir: Path) -> None:
    _executable(bindir, "qsvlite")
    dp = _executable(bindir, "qsvdp")
    assert Path(find_qsv()) == dp.absolute()


def test_find_qsv_uses_qsv_bin(bindir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _executable(bindir, "qsv")
    lite = _executable(bindir, "qsvlite")
    monkeypatch.setenv("QSV_BIN", "qsvlite")
    assert Path(find_qsv()) == lite.absolute()


def test_find_qsv_explicit_binary_beats_qsv_bin(
    bindir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite = _executable(bindir, "qsvlite")
    monkeypatch.setenv("QSV_BIN", "qsv-missing")
    assert Path(find_qsv(lite)) == lite.absolute()


def test_find_qsv_relative_path_is_made_absolute(
    bindir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite = _executable(bindir, "qsvlite")
    monkeypatch.chdir(bindir.parent)
    assert find_qsv(os.path.join("bin", lite.name)) == str(lite.absolute())


def test_find_qsv_not_found(bindir: Path, tmp_path: Path) -> None:
    with pytest.raises(QsvNotFound, match="tried qsv, qsvmcp, qsvdp, qsvlite"):
        find_qsv()
    with pytest.raises(QsvNotFound):
        find_qsv(tmp_path / "nope" / "qsv")
    with pytest.raises(QsvNotFound):
        Qsv(tmp_path / "nope" / "qsv")


def test_find_qsv_windows_path_without_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_process, "IS_WINDOWS", True)
    monkeypatch.setenv("PATHEXT", os.pathsep.join([".com", ".exe"]))
    exe = tmp_path / "qsv.exe"
    exe.write_text("")
    assert Path(find_qsv(tmp_path / "qsv")) == exe.absolute()


# -- structured (JSON) errors through run() -------------------------------------------------

# Like qsv after 24.0.0: one JSON line last on stderr when QSV_ERROR_FORMAT=json (the client
# always sets it), free text otherwise. Earlier stderr lines are not part of the error.
JSON_ERROR = """
import json, os, sys
kind, code = os.environ["FAKE_KIND"], int(os.environ["FAKE_CODE"])
sys.stderr.write("some progress output\\n")
if os.environ.get("QSV_ERROR_FORMAT", "").strip().lower() == "json":
    err = {"kind": kind, "level": "error", "message": "it went wrong", "exit_code": code,
           "command": "select", "qsv_version": "24.1.0"}
    sys.stderr.write(json.dumps({"error": err}) + "\\n")
else:
    sys.stderr.write(kind + " error: it went wrong\\n")
sys.exit(code)
"""

KINDS: list[tuple[str, int, type[QsvError]]] = [
    ("csv", 1, QsvCsvError),
    ("io", 1, QsvIOError),
    ("no_match", 1, QsvNoMatch),
    ("usage", 2, QsvUsageError),
    ("other", 1, QsvError),
]


def _check_structured(exc: QsvError, kind: str, code: int, cls: type[QsvError]) -> None:
    assert type(exc) is cls
    assert exc.structured
    assert (exc.kind, exc.exit_code, exc.command, exc.message) == (
        kind,
        code,
        "select",
        "it went wrong",
    )


@pytest.mark.parametrize(("kind", "code", "cls"), KINDS)
def test_json_error_maps_to_class(
    fake_qsv: Callable[[str], str], kind: str, code: int, cls: type[QsvError]
) -> None:
    qsv = Qsv(fake_qsv(JSON_ERROR))
    with pytest.raises(QsvError) as exc:
        qsv.run("select", "a", env={"FAKE_KIND": kind, "FAKE_CODE": str(code)})
    _check_structured(exc.value, kind, code, cls)


@pytest.mark.parametrize(("kind", "code", "cls"), KINDS)
async def test_async_json_error_maps_to_class(
    fake_qsv: Callable[[str], str], kind: str, code: int, cls: type[QsvError]
) -> None:
    qsv = AsyncQsv(fake_qsv(JSON_ERROR))
    with pytest.raises(QsvError) as exc:
        await qsv.run("select", "a", env={"FAKE_KIND": kind, "FAKE_CODE": str(code)})
    _check_structured(exc.value, kind, code, cls)


def test_json_error_with_check_false_returns_result(fake_qsv: Callable[[str], str]) -> None:
    res = Qsv(fake_qsv(JSON_ERROR)).run(
        "select", env={"FAKE_KIND": "csv", "FAKE_CODE": "1"}, check=False
    )
    assert not res.ok
    assert res.exit_code == 1
    assert '"kind": "csv"' in res.stderr


# -- capabilities from --capabilities JSON --------------------------------------------------

# --version and --list fail, so a fallback away from --capabilities shows up as an error
CAPABILITIES = """
import json, os, sys
open(os.environ["FAKE_CALLS"], "a").write(sys.argv[1] + "\\n")
if sys.argv[1] == "--capabilities":
    print(json.dumps({
        "binary": "qsv",
        "version": "24.1.0",
        "features": ["geocode", "polars"],
        "feature_versions": {"polars": "0.55.1"},
        "commands": ["count", "select", "stats"],
        "error_formats": ["text", "json"],
        "target": "x86_64-unknown-linux-gnu",
    }))
elif sys.argv[1] in ("--version", "--list"):
    sys.exit(99)
else:
    print("3")
"""


def _check_caps(caps: qsv_client.Capabilities) -> None:
    assert (caps.binary, caps.version, caps.version_tuple) == ("qsv", "24.1.0", (24, 1, 0))
    assert caps.supports_json_errors
    assert caps.has_feature("polars") and not caps.has_feature("luau")
    assert caps.feature_versions == {"polars": "0.55.1"}
    assert caps.has_command("stats") and not caps.has_command("describegpt")
    assert caps.target == "x86_64-unknown-linux-gnu"
    assert caps.raw is not None


def test_capabilities_from_json_are_probed_once(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    qsv = Qsv(fake_qsv(CAPABILITIES), env={"FAKE_CALLS": str(calls)})
    _check_caps(qsv.capabilities)
    assert qsv.version == "24.1.0"
    assert qsv.capabilities is qsv.capabilities
    assert calls.read_text().split() == ["--capabilities"]


async def test_async_capabilities_from_json_are_probed_once(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    qsv = AsyncQsv(fake_qsv(CAPABILITIES), env={"FAKE_CALLS": str(calls)})
    _check_caps(await qsv.capabilities())
    assert await qsv.version() == "24.1.0"
    assert await qsv.capabilities() is await qsv.capabilities()
    assert calls.read_text().split() == ["--capabilities"]


def test_min_version_from_capabilities_json(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    env = {"FAKE_CALLS": str(tmp_path / "calls.txt")}
    binary = fake_qsv(CAPABILITIES)
    assert Qsv(binary, env=env, min_version="24.1.0").count("x.csv") == 3
    with pytest.raises(QsvVersionError, match=r"at least 25\.0\.0"):
        Qsv(binary, env=env, min_version="25.0.0").count("x.csv")


async def test_async_min_version_from_capabilities_json(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    env = {"FAKE_CALLS": str(tmp_path / "calls.txt")}
    binary = fake_qsv(CAPABILITIES)
    assert await AsyncQsv(binary, env=env, min_version="24.1.0").count("x.csv") == 3
    with pytest.raises(QsvVersionError, match=r"at least 25\.0\.0"):
        await AsyncQsv(binary, env=env, min_version="25.0.0").count("x.csv")


# --capabilities JSON without a usable version; --version is a valid qsvlite line
UNUSABLE_CAPABILITIES_VERSION = """
import json, os, sys
if sys.argv[1] == "--capabilities":
    print(json.dumps({"binary": "qsv", "version": json.loads(os.environ["FAKE_VERSION"])}))
elif sys.argv[1] == "--version":
    print("qsvlite 24.0.0-standard--4-4;1 GiB-0 B-2 GiB-4 GiB "
          "(x86_64-unknown-linux-gnu compiled with Rust 1.99;x-y;z-2) compiled")
else:
    print("3")
"""


@pytest.mark.parametrize("version", ["null", '"dev"', '""'])
def test_unusable_capabilities_version_falls_back_to_version(
    fake_qsv: Callable[[str], str], version: str
) -> None:
    qsv = Qsv(
        fake_qsv(UNUSABLE_CAPABILITIES_VERSION),
        env={"FAKE_VERSION": version},
        min_version="24.0.0",
    )
    assert qsv.count("x.csv") == 3
    assert (qsv.capabilities.binary, qsv.version) == ("qsvlite", "24.0.0")


@pytest.mark.parametrize("version", ["null", '"dev"', '""'])
async def test_async_unusable_capabilities_version_falls_back_to_version(
    fake_qsv: Callable[[str], str], version: str
) -> None:
    qsv = AsyncQsv(
        fake_qsv(UNUSABLE_CAPABILITIES_VERSION),
        env={"FAKE_VERSION": version},
        min_version="24.0.0",
    )
    assert await qsv.count("x.csv") == 3
    assert (await qsv.version()) == "24.0.0"
