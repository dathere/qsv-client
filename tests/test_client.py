"""Client-level unit tests: binary discovery, JSON errors and the --capabilities probe
(emulated with fake_qsv, in the shapes qsv's own tests/test_error_format.rs pins)."""

from __future__ import annotations

import asyncio
import gc
import importlib.metadata
import os
import stat
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
    QsvTimeout,
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

# Like qsv 25.0.0+: one JSON line last on stderr when QSV_ERROR_FORMAT=json (the client
# always sets it), free text otherwise. Earlier stderr lines are not part of the error.
JSON_ERROR = """
import json, os, sys
kind, code = os.environ["FAKE_KIND"], int(os.environ["FAKE_CODE"])
sys.stderr.write("some progress output\\n")
if os.environ.get("QSV_ERROR_FORMAT", "").strip().lower() == "json":
    err = {"kind": kind, "level": "error", "message": "it went wrong", "exit_code": code,
           "command": "select", "qsv_version": "25.1.0"}
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

# the probe is logged to FAKE_CALLS, so a second probe (or a stray --version) shows up
CAPABILITIES = """
import json, os, sys
open(os.environ["FAKE_CALLS"], "a").write(sys.argv[1] + "\\n")
if sys.argv[1] == "--capabilities":
    print(json.dumps({
        "binary": "qsv",
        "version": os.environ.get("FAKE_VERSION", "25.1.0"),
        "features": ["geocode", "polars"],
        "feature_versions": {"polars": "0.55.1"},
        "commands": ["count", "select", "stats"],
        "error_formats": ["text", "json"],
        "target": "x86_64-unknown-linux-gnu",
    }))
else:
    print("3")
"""


def _check_caps(caps: qsv_client.Capabilities) -> None:
    assert (caps.binary, caps.version, caps.version_tuple) == ("qsv", "25.1.0", (25, 1, 0))
    assert caps.has_feature("polars") and not caps.has_feature("luau")
    assert caps.feature_versions == {"polars": "0.55.1"}
    assert caps.has_command("stats") and not caps.has_command("describegpt")
    assert caps.target == "x86_64-unknown-linux-gnu"
    assert caps.raw["error_formats"] == ["text", "json"]


def test_capabilities_from_json_are_probed_once(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    qsv = Qsv(fake_qsv(CAPABILITIES), env={"FAKE_CALLS": str(calls)})
    _check_caps(qsv.capabilities)
    assert qsv.version == "25.1.0"
    assert qsv.capabilities is qsv.capabilities
    assert qsv.count("x.csv") == 3
    assert calls.read_text().split() == ["--capabilities", "count"]


async def test_async_capabilities_from_json_are_probed_once(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    qsv = AsyncQsv(fake_qsv(CAPABILITIES), env={"FAKE_CALLS": str(calls)})
    _check_caps(await qsv.capabilities())
    assert await qsv.version() == "25.1.0"
    assert await qsv.capabilities() is await qsv.capabilities()
    assert await qsv.count("x.csv") == 3
    assert calls.read_text().split() == ["--capabilities", "count"]


def test_min_version_from_capabilities_json(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    env = {"FAKE_CALLS": str(tmp_path / "calls.txt")}
    binary = fake_qsv(CAPABILITIES)
    assert Qsv(binary, env=env, min_version="25.1.0").count("x.csv") == 3
    with pytest.raises(QsvVersionError, match=r"at least 25\.2\.0"):
        Qsv(binary, env=env, min_version="25.2.0").count("x.csv")


async def test_async_min_version_from_capabilities_json(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    env = {"FAKE_CALLS": str(tmp_path / "calls.txt")}
    binary = fake_qsv(CAPABILITIES)
    assert await AsyncQsv(binary, env=env, min_version="25.1.0").count("x.csv") == 3
    with pytest.raises(QsvVersionError, match=r"at least 25\.2\.0"):
        await AsyncQsv(binary, env=env, min_version="25.2.0").count("x.csv")


def test_capabilities_are_shared_across_clients(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(CAPABILITIES)
    env = {"FAKE_CALLS": str(calls)}
    for _ in range(3):
        assert Qsv(binary, env=env).count("x.csv") == 3
    # min_version is still checked per client against the shared result
    with pytest.raises(QsvVersionError, match=r"at least 25\.2\.0"):
        Qsv(binary, env=env, min_version="25.2.0").count("x.csv")
    assert calls.read_text().split() == ["--capabilities", "count", "count", "count"]


async def test_async_capabilities_are_shared_across_clients(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(CAPABILITIES)
    env = {"FAKE_CALLS": str(calls)}
    assert Qsv(binary, env=env).count("x.csv") == 3
    for _ in range(2):
        assert await AsyncQsv(binary, env=env).count("x.csv") == 3
    with pytest.raises(QsvVersionError, match=r"at least 25\.2\.0"):
        await AsyncQsv(binary, env=env, min_version="25.2.0").count("x.csv")
    assert calls.read_text().split() == ["--capabilities", "count", "count", "count"]


def test_cached_capabilities_are_private_to_each_client(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    binary = fake_qsv(CAPABILITIES)
    env = {"FAKE_CALLS": str(tmp_path / "calls.txt")}
    first = Qsv(binary, env=env).capabilities
    first.raw["commands"].append("hacked")
    first.raw["injected"] = True
    first.feature_versions["polars"] = "0.0.0"
    for caps in (Qsv(binary, env=env).capabilities, Qsv(binary, env=env).capabilities):
        assert "hacked" not in caps.raw["commands"] and "injected" not in caps.raw
        assert caps.feature_versions == {"polars": "0.55.1"}


async def test_async_cached_capabilities_are_private_to_each_client(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    binary = fake_qsv(CAPABILITIES)
    env = {"FAKE_CALLS": str(tmp_path / "calls.txt")}
    first = await AsyncQsv(binary, env=env).capabilities()
    first.raw["injected"] = True
    first.feature_versions["polars"] = "0.0.0"
    caps = await AsyncQsv(binary, env=env).capabilities()
    assert "injected" not in caps.raw
    assert caps.feature_versions == {"polars": "0.55.1"}


def test_replaced_binary_is_probed_again(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(CAPABILITIES)
    assert Qsv(binary, env={"FAKE_CALLS": str(calls)}).version == "25.1.0"
    # an in-place upgrade: same path, new mtime (on Windows `binary` is an unchanged .cmd
    # wrapper, so set it explicitly rather than rely on the rewrite)
    fake_qsv(CAPABILITIES + "\n# upgraded\n")
    st = os.stat(binary)
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert Qsv(binary, env={"FAKE_CALLS": str(calls)}).version == "25.1.0"
    assert calls.read_text().split() == ["--capabilities", "--capabilities"]


def test_too_old_version_is_cached_and_rechecked(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(CAPABILITIES)
    env = {"FAKE_CALLS": str(calls), "FAKE_VERSION": "24.9.0"}
    for _ in range(2):
        with pytest.raises(QsvVersionError):
            Qsv(binary, env=env).count("x.csv")
    assert calls.read_text().split() == ["--capabilities"]


# rejects --capabilities like qsv 24.0.0, logging each call
LOGGED_PRE_CAPABILITIES = """
import os, sys
open(os.environ["FAKE_CALLS"], "a").write(sys.argv[1] + "\\n")
if sys.argv[1] == "--capabilities":
    sys.stderr.write("Unknown flag: '--capabilities'\\n")
    sys.exit(1)
print("3")
"""


def test_failed_probe_is_not_cached(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(LOGGED_PRE_CAPABILITIES)
    for _ in range(2):
        with pytest.raises(QsvVersionError):
            Qsv(binary, env={"FAKE_CALLS": str(calls)}).count("x.csv")
    assert calls.read_text().split() == ["--capabilities", "--capabilities"]


# the probe sleeps, so concurrent first calls overlap it
SLOW_CAPABILITIES = """
import os, sys, time
open(os.environ["FAKE_CALLS"], "a").write(sys.argv[1] + "\\n")
if sys.argv[1] == "--capabilities":
    time.sleep(0.5)
    print('{"binary": "qsv", "version": "25.1.0"}')
else:
    print("3")
"""


def test_concurrent_first_calls_probe_once(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(SLOW_CAPABILITIES)
    env = {"FAKE_CALLS": str(calls)}
    shared = Qsv(binary, env=env)
    clients = [shared, shared, Qsv(binary, env=env), Qsv(binary, env=env)]
    with ThreadPoolExecutor(len(clients)) as pool:
        assert list(pool.map(lambda q: q.count("x.csv"), clients)) == [3] * len(clients)
    assert calls.read_text().split().count("--capabilities") == 1


async def test_async_concurrent_first_calls_probe_once(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(SLOW_CAPABILITIES)
    env = {"FAKE_CALLS": str(calls)}
    shared = AsyncQsv(binary, env=env)
    clients = [shared, shared, AsyncQsv(binary, env=env), AsyncQsv(binary, env=env)]
    assert await asyncio.gather(*(q.count("x.csv") for q in clients)) == [3] * len(clients)
    assert calls.read_text().split().count("--capabilities") == 1


# --capabilities hangs; the run's timeout must bound it
HUNG_CAPABILITIES = """
import sys, time
if sys.argv[1] == "--capabilities":
    time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 30)
print("3")
"""


def test_run_timeout_bounds_the_probe(fake_qsv: Callable[[str], str]) -> None:
    qsv = Qsv(fake_qsv(HUNG_CAPABILITIES), kill_grace=0.5)
    started = time.monotonic()
    with pytest.raises(QsvTimeout):
        qsv.run("count", "x.csv", timeout=0.5)
    assert time.monotonic() - started < 10


async def test_async_run_timeout_bounds_the_probe(fake_qsv: Callable[[str], str]) -> None:
    qsv = AsyncQsv(fake_qsv(HUNG_CAPABILITIES), kill_grace=0.5)
    started = time.monotonic()
    with pytest.raises(QsvTimeout):
        await qsv.run("count", "x.csv", timeout=0.5)
    assert time.monotonic() - started < 10


def test_run_timeout_bounds_waiting_for_another_probe(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    calls = tmp_path / "calls.txt"
    binary = fake_qsv(SLOW_CAPABILITIES)
    env = {"FAKE_CALLS": str(calls)}
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(lambda: Qsv(binary, env=env).count("x.csv"))
        while not calls.exists():  # the first client's probe is running
            time.sleep(0.01)
        with pytest.raises(QsvTimeout, match="waiting for another client"):
            Qsv(binary, env=env).run("count", "x.csv", timeout=0.1)
        assert first.result() == 3


async def test_async_run_timeout_bounds_waiting_for_another_probe(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    binary = fake_qsv(SLOW_CAPABILITIES)
    env = {"FAKE_CALLS": str(tmp_path / "calls.txt")}
    first = asyncio.ensure_future(AsyncQsv(binary, env=env).count("x.csv"))
    await asyncio.sleep(0.1)  # the first client holds the probe lock
    with pytest.raises(QsvTimeout, match="waiting for another client"):
        await AsyncQsv(binary, env=env).run("count", "x.csv", timeout=0.1)
    assert await first == 3


# a supported qsv whose probe fails for another reason (crash, startup error)
CRASHING_PROBE = """
import sys
if sys.argv[1] == "--capabilities":
    sys.stderr.write("error while loading shared libraries: libfoo.so\\n")
    sys.exit(127)
print("3")
"""


def test_probe_failure_is_not_a_version_error(fake_qsv: Callable[[str], str]) -> None:
    with pytest.raises(QsvError) as exc:
        Qsv(fake_qsv(CRASHING_PROBE)).count("x.csv")
    assert not isinstance(exc.value, QsvVersionError)
    assert exc.value.exit_code == 127
    assert "libfoo.so" in exc.value.stderr


# a wrapper that traces the command line (mentioning the flag) and then fails to start qsv
TRACING_WRAPPER_FAILURE = """
import sys
if sys.argv[1] == "--capabilities":
    sys.stderr.write("+ exec qsv --capabilities\\n")
    sys.stderr.write("error while loading shared libraries: libfoo.so\\n")
    sys.exit(127)
print("3")
"""


def test_probe_failure_mentioning_the_flag_is_not_a_version_error(
    fake_qsv: Callable[[str], str],
) -> None:
    with pytest.raises(QsvError) as exc:
        Qsv(fake_qsv(TRACING_WRAPPER_FAILURE)).count("x.csv")
    assert not isinstance(exc.value, QsvVersionError)
    assert exc.value.exit_code == 127
    assert "libfoo.so" in exc.value.stderr


async def test_async_probe_failure_is_not_a_version_error(
    fake_qsv: Callable[[str], str],
) -> None:
    with pytest.raises(QsvError) as exc:
        await AsyncQsv(fake_qsv(CRASHING_PROBE)).count("x.csv")
    assert not isinstance(exc.value, QsvVersionError)
    assert exc.value.exit_code == 127
    assert "libfoo.so" in exc.value.stderr


# rejects --capabilities slowly, so concurrent callers contend for the probe lock
SLOW_PRE_CAPABILITIES = """
import sys, time
if sys.argv[1] == "--capabilities":
    time.sleep(0.2)
    sys.stderr.write("Unknown flag: '--capabilities'\\n")
    sys.exit(1)
print("3")
"""


def test_contended_async_probe_locks_do_not_retain_event_loops(
    fake_qsv: Callable[[str], str],
) -> None:
    binary = fake_qsv(SLOW_PRE_CAPABILITIES)

    async def contend() -> None:
        results = await asyncio.gather(
            AsyncQsv(binary).count("x.csv"),
            AsyncQsv(binary).count("x.csv"),
            return_exceptions=True,
        )
        assert all(isinstance(r, QsvVersionError) for r in results)

    for _ in range(3):
        asyncio.run(contend())
    gc.collect()
    assert len(qsv_client.client._async_probe_locks) == 0


# min_version can raise the 25.0.0 floor, never lower it
@pytest.mark.parametrize("min_version", [None, "1.0.0"])
def test_floor_is_enforced_before_first_run(
    fake_qsv: Callable[[str], str], tmp_path: Path, min_version: str | None
) -> None:
    calls = tmp_path / "calls.txt"
    env = {"FAKE_CALLS": str(calls), "FAKE_VERSION": "24.9.0"}
    qsv = Qsv(fake_qsv(CAPABILITIES), env=env, min_version=min_version)
    # `except QsvError` around run() also catches a too-old binary
    with pytest.raises(QsvError, match=r"qsv 24\.9\.0; at least 25\.0\.0") as e:
        qsv.count("x.csv")
    assert type(e.value) is QsvVersionError
    assert (e.value.kind, e.value.exit_code) == ("version", 0)
    assert str(e.value) == e.value.message
    assert calls.read_text().split() == ["--capabilities"]


@pytest.mark.parametrize("min_version", [None, "1.0.0"])
async def test_async_floor_is_enforced_before_first_run(
    fake_qsv: Callable[[str], str], tmp_path: Path, min_version: str | None
) -> None:
    calls = tmp_path / "calls.txt"
    env = {"FAKE_CALLS": str(calls), "FAKE_VERSION": "24.9.0"}
    qsv = AsyncQsv(fake_qsv(CAPABILITIES), env=env, min_version=min_version)
    with pytest.raises(QsvVersionError, match=r"qsv 24\.9\.0; at least 25\.0\.0"):
        await qsv.count("x.csv")
    assert calls.read_text().split() == ["--capabilities"]


# qsv 24.0.0 and earlier reject --capabilities as an unknown flag
PRE_CAPABILITIES = """
import sys
if sys.argv[1] == "--capabilities":
    sys.stderr.write("Unknown flag: '--capabilities'\\n")
    sys.exit(1)
print("3")
"""


def test_binary_without_capabilities_is_too_old(fake_qsv: Callable[[str], str]) -> None:
    qsv = Qsv(fake_qsv(PRE_CAPABILITIES))
    with pytest.raises(QsvVersionError, match=r"does not support --capabilities.*25\.0\.0") as e:
        qsv.count("x.csv")
    # a QsvError, describing the --capabilities check that rejected the binary
    assert isinstance(e.value, QsvError)
    assert (e.value.kind, e.value.exit_code) == ("version", 1)
    assert e.value.args_run[1:] == ("--capabilities",)
    assert "Unknown flag: '--capabilities'" in e.value.stderr
    with pytest.raises(QsvVersionError):
        _ = qsv.capabilities


async def test_async_binary_without_capabilities_is_too_old(
    fake_qsv: Callable[[str], str],
) -> None:
    qsv = AsyncQsv(fake_qsv(PRE_CAPABILITIES))
    with pytest.raises(QsvVersionError, match=r"does not support --capabilities.*25\.0\.0"):
        await qsv.count("x.csv")
    with pytest.raises(QsvVersionError):
        await qsv.capabilities()


# --capabilities JSON without a usable version
UNUSABLE_CAPABILITIES_VERSION = """
import json, os, sys
if sys.argv[1] == "--capabilities":
    print(json.dumps({"binary": "qsv", "version": json.loads(os.environ["FAKE_VERSION"])}))
else:
    print("3")
"""


@pytest.mark.parametrize("version", ["null", '"dev"', '""'])
def test_unusable_capabilities_version_raises(fake_qsv: Callable[[str], str], version: str) -> None:
    qsv = Qsv(fake_qsv(UNUSABLE_CAPABILITIES_VERSION), env={"FAKE_VERSION": version})
    with pytest.raises(QsvError, match="could not determine the qsv version"):
        qsv.count("x.csv")


@pytest.mark.parametrize("version", ["null", '"dev"', '""'])
async def test_async_unusable_capabilities_version_raises(
    fake_qsv: Callable[[str], str], version: str
) -> None:
    qsv = AsyncQsv(fake_qsv(UNUSABLE_CAPABILITIES_VERSION), env={"FAKE_VERSION": version})
    with pytest.raises(QsvError, match="could not determine the qsv version"):
        await qsv.count("x.csv")
