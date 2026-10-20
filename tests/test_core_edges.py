"""Edge cases of the core library: the Windows stdin feeder run on POSIX, teardown races,
helper parsers, and the parts of the client API not exercised elsewhere with a fake qsv."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_process import (
    BIG_STDIN,
    HANG_WITH_ESCAPED_GRANDCHILD,
    HANG_WITHOUT_READING,
    IGNORE_STOP_AND_HANG,
    _kill,
    _read_pid,
    _wait_dead,
)

from qsv_client import (
    AsyncQsv,
    Qsv,
    QsvNotFound,
    QsvResult,
    QsvTimeout,
    QsvUsageError,
    _process,
    client,
)
from qsv_client.capabilities import from_capabilities_json
from qsv_client.errors import parse_json_error

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-specific setup")

# --- _process.py: the stdin feeder thread (Windows-only in production, forced on here) ---

ECHO_STDIN = """
import sys
sys.stdout.write(sys.stdin.read())
"""


@posix_only
def test_stdin_feeder_delivers_stdin(
    fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    qsv = Qsv(fake_qsv(ECHO_STDIN))
    _ = qsv.capabilities  # probe before forcing the Windows path
    monkeypatch.setattr(_process, "IS_WINDOWS", True)
    data = "a,b\n" + "1,2\n" * 50_000  # larger than a pipe buffer
    assert qsv.run("cat", stdin=data).stdout == data


@posix_only
def test_stdin_feeder_survives_child_that_exits_without_reading(
    fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the write hits a broken pipe; the feeder swallows it and the run still completes
    qsv = Qsv(fake_qsv("print('done')"))
    _ = qsv.capabilities
    monkeypatch.setattr(_process, "IS_WINDOWS", True)
    assert qsv.run("x", stdin=BIG_STDIN).stdout == "done\n"


@posix_only
def test_stdin_feeder_timeout_bounded_when_stdin_is_not_read(
    fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    qsv = Qsv(fake_qsv(HANG_WITHOUT_READING), kill_grace=0.5)
    _ = qsv.capabilities
    monkeypatch.setattr(_process, "IS_WINDOWS", True)
    started = time.monotonic()
    with pytest.raises(QsvTimeout):
        qsv.run("stats", stdin=BIG_STDIN, timeout=1)
    assert time.monotonic() - started < 10


# --- _process.py: teardown races ---


def test_timeout_with_stdout_path_bounded_when_grandchild_escapes_group(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    # stdout goes to a file, so only stderr is a pipe the escaped grandchild can hold open
    pidfile = tmp_path / "child.pid"
    qsv = Qsv(fake_qsv(HANG_WITH_ESCAPED_GRANDCHILD), kill_grace=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(QsvTimeout):
            qsv.run("stats", pidfile, stdout_path=tmp_path / "out.csv", timeout=1)
        assert time.monotonic() - started < 10
    finally:
        _kill(_read_pid(pidfile))


async def test_async_timeout_with_stdout_path_bounded_when_grandchild_escapes_group(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    qsv = AsyncQsv(fake_qsv(HANG_WITH_ESCAPED_GRANDCHILD), kill_grace=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(QsvTimeout):
            await qsv.run("stats", pidfile, stdout_path=tmp_path / "out.csv", timeout=1)
        assert time.monotonic() - started < 10
    finally:
        _kill(_read_pid(pidfile))


def test_asyncio_run_cancelling_reap_of_cancelled_run_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    # the run is cancelled and starts its 2s SIGTERM grace wait; main() returns meanwhile, so
    # the runner's shutdown cancels the reap itself, which must still SIGKILL the leader
    pidfile = tmp_path / "leader.pid"
    qsv = AsyncQsv(fake_qsv(IGNORE_STOP_AND_HANG), kill_grace=2)

    async def main() -> None:
        background = asyncio.create_task(qsv.run("stats", pidfile))
        await asyncio.to_thread(_read_pid, pidfile)
        background.cancel()
        await asyncio.sleep(0.3)
        assert not background.done()  # still in its SIGTERM grace wait

    try:
        asyncio.run(main())
        assert _wait_dead(_read_pid(pidfile), 6), "leader survived the runner's shutdown"
    finally:
        _kill(_read_pid(pidfile))


# --- client.py ---


def test_result_without_captured_stdout_cannot_be_parsed() -> None:
    # not producible by run() (it always sets one of the three), but QsvResult is public
    res = QsvResult(
        args=("qsv",), exit_code=0, stdout=None, stdout_bytes=None, stderr="", duration=0.0
    )
    with pytest.raises(ValueError, match="captured no stdout"):
        res.json()
    with pytest.raises(ValueError, match="captured no stdout"):
        res.csv_rows()


def test_result_parses_stdout_bytes() -> None:
    res = QsvResult(
        args=("qsv",), exit_code=0, stdout=None, stdout_bytes=b"a,b\n1,2\n", stderr="", duration=0
    )
    assert res.csv_rows() == [{"a": "1", "b": "2"}]


@pytest.mark.parametrize("cls", [Qsv, AsyncQsv])
def test_repr_shows_binary_but_not_secrets(
    fake_qsv: Callable[[str], str], cls: type[Qsv] | type[AsyncQsv]
) -> None:
    fake = fake_qsv("pass")
    text = repr(cls(fake, llm_api_key="sk-secret", env={"TOKEN": "hush"}))
    assert text == f"{cls.__name__}(binary={fake!r})"
    assert "sk-secret" not in text and "hush" not in text


ECHO_SYSTEMROOT = """
import json, os
print(json.dumps({k: os.environ.get(k) for k in ["SYSTEMROOT", "EXTRA"]}))
"""


def test_windows_keeps_systemroot_without_inherit_env(
    fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_process, "IS_WINDOWS", True)
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setenv("EXTRA", "from-parent")
    out = Qsv(fake_qsv(ECHO_SYSTEMROOT), inherit_env=False).run("x").json()
    assert out == {"SYSTEMROOT": "C:\\Windows", "EXTRA": None}


async def test_async_windows_keeps_systemroot_without_inherit_env(
    fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_process, "IS_WINDOWS", True)
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setenv("EXTRA", "from-parent")
    out = (await AsyncQsv(fake_qsv(ECHO_SYSTEMROOT), inherit_env=False).run("x")).json()
    assert out == {"SYSTEMROOT": "C:\\Windows", "EXTRA": None}


def test_systemroot_not_added_off_windows(
    fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_process, "IS_WINDOWS", False)
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    out = Qsv(fake_qsv(ECHO_SYSTEMROOT), inherit_env=False).run("x").json()
    assert out["SYSTEMROOT"] is None


def test_binary_removed_after_construction(fake_qsv: Callable[[str], str]) -> None:
    fake = fake_qsv("print('ok')")
    qsv = Qsv(fake)
    os.remove(fake)
    # still a FileNotFoundError, so callers' existing OSError handlers catch it
    with pytest.raises(FileNotFoundError) as info:
        qsv.run("x")
    assert isinstance(info.value, QsvNotFound)
    assert "no longer exists" in str(info.value)
    assert client._caps_cache == {}


async def test_async_binary_removed_after_construction(fake_qsv: Callable[[str], str]) -> None:
    fake = fake_qsv("print('ok')")
    qsv = AsyncQsv(fake)
    os.remove(fake)
    with pytest.raises(QsvNotFound, match="no longer exists"):
        await qsv.run("x")
    assert client._caps_cache == {}


def test_missing_cwd_is_not_qsv_not_found(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    qsv = Qsv(fake_qsv("print('ok')"), cwd=tmp_path / "gone")
    # FileNotFoundError on POSIX, NotADirectoryError on Windows; never QsvNotFound
    with pytest.raises(OSError) as info:
        qsv.run("x")
    assert not isinstance(info.value, QsvNotFound)


async def test_async_missing_cwd_is_not_qsv_not_found(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    qsv = AsyncQsv(fake_qsv("print('ok')"), cwd=tmp_path / "gone")
    # FileNotFoundError on POSIX, NotADirectoryError on Windows; never QsvNotFound
    with pytest.raises(OSError) as info:
        await qsv.run("x")
    assert not isinstance(info.value, QsvNotFound)


# prints a CSV naming the subcommand and its arguments, or the argv as JSON for describegpt
ECHO_AS_CSV_OR_JSON = """
import json, sys
args = sys.argv[1:]
if args[0] == "describegpt":
    print(json.dumps({"argv": args}))
else:
    print("command,args")
    print(args[0] + "," + " ".join(args[1:]))
"""


def test_stats_frequency_describegpt(fake_qsv: Callable[[str], str]) -> None:
    qsv = Qsv(fake_qsv(ECHO_AS_CSV_OR_JSON))
    assert qsv.stats("in.csv", "--everything") == [
        {"command": "stats", "args": "in.csv --everything"}
    ]
    assert qsv.frequency("in.csv") == [{"command": "frequency", "args": "in.csv"}]
    assert qsv.describegpt("in.csv", "--all") == {
        "argv": ["describegpt", "in.csv", "--all", "--format", "json"]
    }
    assert qsv.describegpt("in.csv", "--format", "json") == {
        "argv": ["describegpt", "in.csv", "--format", "json"]
    }


async def test_async_stats_frequency_describegpt(fake_qsv: Callable[[str], str]) -> None:
    qsv = AsyncQsv(fake_qsv(ECHO_AS_CSV_OR_JSON))
    assert await qsv.stats("in.csv", "--everything") == [
        {"command": "stats", "args": "in.csv --everything"}
    ]
    assert await qsv.frequency("in.csv") == [{"command": "frequency", "args": "in.csv"}]
    assert await qsv.describegpt("in.csv", "--all") == {
        "argv": ["describegpt", "in.csv", "--all", "--format", "json"]
    }
    assert await qsv.describegpt("in.csv", "--format=json") == {
        "argv": ["describegpt", "in.csv", "--format=json"]
    }


# --- capabilities.py / errors.py ---


@pytest.mark.parametrize("text", ["[]", '"25.0.0"', '{"binary": "qsv"}'])
def test_from_capabilities_json_rejects_other_documents(text: str) -> None:
    with pytest.raises(ValueError, match="not a qsv --capabilities document"):
        from_capabilities_json(text)


ERROR_LINE = json.dumps({"error": {"kind": "usage", "message": "bad flag"}})


def test_parse_json_error_skips_trailing_blank_lines() -> None:
    assert parse_json_error(f"warning: x\n{ERROR_LINE}\n\n  \n") == {
        "kind": "usage",
        "message": "bad flag",
    }
    assert parse_json_error("\n\n") is None


USAGE_ERROR_THEN_BLANK = f"""
import sys
sys.stderr.write({ERROR_LINE!r} + "\\n\\n")
sys.exit(1)
"""


def test_error_line_followed_by_blank_lines_keeps_its_kind(
    fake_qsv: Callable[[str], str],
) -> None:
    with pytest.raises(QsvUsageError, match="bad flag"):
        Qsv(fake_qsv(USAGE_ERROR_THEN_BLANK)).run("x")


async def test_async_error_line_followed_by_blank_lines_keeps_its_kind(
    fake_qsv: Callable[[str], str],
) -> None:
    with pytest.raises(QsvUsageError, match="bad flag"):
        await AsyncQsv(fake_qsv(USAGE_ERROR_THEN_BLANK)).run("x")
