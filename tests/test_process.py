"""Process control, exercised with a fake qsv so timing is deterministic."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from qsv_client import AsyncQsv, Qsv, QsvTimeout, QsvUsageError

# The fake starts a grandchild that inherits stdout and records its pid, then hangs. If only
# the fake were killed, the grandchild would keep the stdout pipe open and the run would hang
# until it exited on its own; killing the process group ends both.
HANG_WITH_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[-1], "w").write(str(child.pid))
time.sleep(60)
"""


def _wait_dead(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _read_pid(path: Path) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("fake never wrote its child pid")


def test_timeout_kills_process_group(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    qsv = Qsv(fake_qsv(HANG_WITH_GRANDCHILD), kill_grace=0.5)
    started = time.monotonic()
    with pytest.raises(QsvTimeout) as exc:
        qsv.run("stats", pidfile, timeout=1)
    assert time.monotonic() - started < 10
    assert exc.value.exit_code == 124
    assert exc.value.kind == "timeout"
    assert _wait_dead(_read_pid(pidfile)), "grandchild survived the timeout"


async def test_async_timeout_kills_process_group(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    qsv = AsyncQsv(fake_qsv(HANG_WITH_GRANDCHILD), kill_grace=0.5)
    with pytest.raises(QsvTimeout):
        await qsv.run("stats", pidfile, timeout=1)
    assert _wait_dead(_read_pid(pidfile)), "grandchild survived the timeout"


async def test_async_cancel_kills_process_group(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    qsv = AsyncQsv(fake_qsv(HANG_WITH_GRANDCHILD), kill_grace=0.5)
    task = asyncio.create_task(qsv.run("stats", pidfile))
    pid = await asyncio.to_thread(_read_pid, pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _wait_dead(pid), "grandchild survived cancellation"


ECHO_ENV_AND_ARGS = """
import json, os, sys
keys = ["QSV_ERROR_FORMAT", "QSV_LLM_APIKEY", "QSV_LLM_BASE_URL", "QSV_LLM_MODEL", "EXTRA"]
print(json.dumps({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in keys},
                  "stdin": sys.stdin.read()}))
"""


def test_env_and_secrets(fake_qsv: Callable[[str], str]) -> None:
    qsv = Qsv(
        fake_qsv(ECHO_ENV_AND_ARGS),
        llm_api_key="sk-secret",
        llm_base_url="https://openrouter.ai/api/v1",
        env={"EXTRA": "client"},
    )
    out = qsv.run("describegpt", "in.csv", env={"EXTRA": "call"}).json()
    assert out["env"] == {
        "QSV_ERROR_FORMAT": "json",
        "QSV_LLM_APIKEY": "sk-secret",
        "QSV_LLM_BASE_URL": "https://openrouter.ai/api/v1",
        "QSV_LLM_MODEL": None,
        "EXTRA": "call",
    }
    assert "sk-secret" not in " ".join(out["argv"])
    assert out["argv"] == ["describegpt", "in.csv"]


def test_inherit_env_false(fake_qsv: Callable[[str], str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRA", "from-parent")
    out = Qsv(fake_qsv(ECHO_ENV_AND_ARGS), inherit_env=False).run("x").json()
    assert out["env"]["EXTRA"] is None
    out = Qsv(fake_qsv(ECHO_ENV_AND_ARGS)).run("x").json()
    assert out["env"]["EXTRA"] == "from-parent"


def test_stdin_and_paths(fake_qsv: Callable[[str], str], tmp_path: Path) -> None:
    qsv = Qsv(fake_qsv(ECHO_ENV_AND_ARGS))
    out = qsv.run("count", tmp_path / "a.csv", 3, stdin="a\n1\n").json()
    assert out["stdin"] == "a\n1\n"
    assert out["argv"] == ["count", str(tmp_path / "a.csv"), "3"]
    # no stdin given -> closed, not inherited from the test runner
    assert qsv.run("count").json()["stdin"] == ""


def test_describegpt_adds_json_format(fake_qsv: Callable[[str], str]) -> None:
    qsv = Qsv(fake_qsv(ECHO_ENV_AND_ARGS))
    assert qsv.describegpt("in.csv", "--all")["argv"] == [
        "describegpt",
        "in.csv",
        "--all",
        "--format",
        "json",
    ]
    assert qsv.describegpt("in.csv", "--format=json")["argv"] == [
        "describegpt",
        "in.csv",
        "--format=json",
    ]


def test_warning_exit_code_is_success(fake_qsv: Callable[[str], str]) -> None:
    res = Qsv(fake_qsv("import sys; print('done'); sys.exit(255)")).run("x")
    assert res.ok
    assert res.exit_code == 255


# Like HANG_WITH_GRANDCHILD, but the grandchild leaves the process group, so the group kill
# misses it and it keeps the output pipes open.
HANG_WITH_ESCAPED_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         start_new_session=True)
open(sys.argv[-1], "w").write(str(child.pid))
time.sleep(60)
"""


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def test_timeout_bounded_when_grandchild_escapes_group(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    qsv = Qsv(fake_qsv(HANG_WITH_ESCAPED_GRANDCHILD), kill_grace=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(QsvTimeout):
            qsv.run("stats", pidfile, timeout=1)
        assert time.monotonic() - started < 10
    finally:
        _kill(_read_pid(pidfile))


async def test_async_timeout_bounded_when_grandchild_escapes_group(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    qsv = AsyncQsv(fake_qsv(HANG_WITH_ESCAPED_GRANDCHILD), kill_grace=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(QsvTimeout):
            await qsv.run("stats", pidfile, timeout=1)
        assert time.monotonic() - started < 10
    finally:
        _kill(_read_pid(pidfile))


def test_undecodable_stdout_still_raises_typed_error(fake_qsv: Callable[[str], str]) -> None:
    qsv = Qsv(fake_qsv("import sys; sys.stdout.buffer.write(b'\\xff'); sys.exit(2)"))
    with pytest.raises(QsvUsageError):
        qsv.run("x")


COUNT_AND_HEADERS = """
import sys
print("3" if sys.argv[1] == "count" else "name\\nn")
"""


def test_count_and_headers_without_captured_text(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    qsv = Qsv(fake_qsv(COUNT_AND_HEADERS))
    kws: list[dict[str, Any]] = [{"stdout_path": tmp_path / "out"}, {"text": False}]
    for kw in kws:
        assert qsv.count("in.csv", **kw) == 3
        assert qsv.headers("in.csv", **kw) == ["name", "n"]


async def test_async_count_and_headers_without_captured_text(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    qsv = AsyncQsv(fake_qsv(COUNT_AND_HEADERS))
    kws: list[dict[str, Any]] = [{"stdout_path": tmp_path / "out"}, {"text": False}]
    for kw in kws:
        assert await qsv.count("in.csv", **kw) == 3
        assert await qsv.headers("in.csv", **kw) == ["name", "n"]


def test_relative_binary_with_cwd(
    fake_qsv: Callable[[str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Path(fake_qsv("print('ok')"))
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(fake.parent)
    assert Qsv(f"./{fake.name}", cwd=other).run("x").stdout == "ok\n"


def test_relative_binary_through_symlinked_dir(
    fake_qsv: Callable[[str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # work/link -> bin, so work/link/../fake-qsv is tmp_path/fake-qsv, not work/fake-qsv
    fake_qsv("print('ok')")
    (tmp_path / "bin").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "link").symlink_to(tmp_path / "bin")
    monkeypatch.chdir(work)
    assert Qsv("link/../fake-qsv").run("x").stdout == "ok\n"


async def test_async_timeout_bounded_when_leader_ignores_sigterm_and_grandchild_escapes(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    body = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    qsv = AsyncQsv(fake_qsv(body + HANG_WITH_ESCAPED_GRANDCHILD), kill_grace=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(QsvTimeout):
            await qsv.run("stats", pidfile, timeout=1)
        assert time.monotonic() - started < 10
    finally:
        _kill(_read_pid(pidfile))


async def test_async_timeout_releases_pipes_held_by_escaped_grandchild(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    qsv = AsyncQsv(fake_qsv(HANG_WITH_ESCAPED_GRANDCHILD), kill_grace=0.3)
    pidfiles = [tmp_path / f"child{i}.pid" for i in range(3)]
    open_fds = len(os.listdir("/dev/fd"))
    try:
        for pidfile in pidfiles:
            with pytest.raises(QsvTimeout):
                await qsv.run("stats", pidfile, timeout=0.5)
        await asyncio.sleep(0)  # asyncio closes pipe fds in a call_soon callback
        assert len(os.listdir("/dev/fd")) <= open_fds
    finally:
        for pidfile in pidfiles:
            _kill(_read_pid(pidfile))
