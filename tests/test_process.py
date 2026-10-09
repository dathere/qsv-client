"""Process control, exercised with a fake qsv so timing is deterministic."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from qsv_client import AsyncQsv, Qsv, QsvTimeout, QsvUsageError, _process

# The fake starts a grandchild that inherits stdout and records its pid, then hangs. If only
# the fake were killed, the grandchild would keep the stdout pipe open and the run would hang
# until it exited on its own; killing the process group ends both.
HANG_WITH_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[-1], "w").write(str(child.pid))
time.sleep(60)
"""


def _is_zombie(pid: int) -> bool:
    """Linux only: dead but not yet reaped. On Python 3.12+ asyncio reaps children from the
    event loop (a pidfd reader), so a child killed as ``asyncio.run()`` shuts down is never
    reaped by it and ``os.kill(pid, 0)`` keeps succeeding."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CloseHandle.restype = wintypes.BOOL
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    def _alive(pid: int) -> bool:
        # not os.kill(pid, 0): on Windows that is TerminateProcess
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = _k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)

else:

    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return not _is_zombie(pid)


def _wait_dead(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
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
# ignore the polite stop: SIGTERM on POSIX, CTRL_BREAK_EVENT (SIGBREAK) on Windows
IGNORE_STOP = """
import signal
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
"""

HANG_WITH_ESCAPED_GRANDCHILD = """
import subprocess, sys, time
# POSIX: its own session, out of the group kill's reach. Windows: its own console process
# group, out of CTRL_BREAK_EVENT's reach (but still inside the run's Job Object).
escape = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
          else {"start_new_session": True})
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **escape)
open(sys.argv[-1], "w").write(str(child.pid))
time.sleep(60)
"""


def _kill(pid: int) -> None:
    # on Windows, os.kill with anything but the console events is TerminateProcess
    with contextlib.suppress(OSError):
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def _check_job_killed(pid: int) -> None:
    """On Windows nothing escapes the Job Object, so the 'escaped' grandchild must be dead."""
    if sys.platform == "win32":
        assert _wait_dead(pid), "the Job Object missed a grandchild in its own process group"


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
        _check_job_killed(_read_pid(pidfile))
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
        _check_job_killed(_read_pid(pidfile))
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


@pytest.mark.skipif(sys.platform == "win32", reason="Win32 collapses `..` before symlinks")
def test_relative_binary_through_symlinked_dir(
    fake_qsv: Callable[[str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # work/link -> bin, so work/link/../fake-qsv is tmp_path/fake-qsv, not work/fake-qsv
    fake = Path(fake_qsv("print('ok')"))
    (tmp_path / "bin").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "link").symlink_to(tmp_path / "bin")
    monkeypatch.chdir(work)
    assert Qsv(f"link/../{fake.name}").run("x").stdout == "ok\n"


async def test_async_timeout_bounded_when_leader_ignores_sigterm_and_grandchild_escapes(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    pidfile = tmp_path / "child.pid"
    qsv = AsyncQsv(fake_qsv(IGNORE_STOP + HANG_WITH_ESCAPED_GRANDCHILD), kill_grace=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(QsvTimeout):
            await qsv.run("stats", pidfile, timeout=1)
        assert time.monotonic() - started < 10
        _check_job_killed(_read_pid(pidfile))
    finally:
        _kill(_read_pid(pidfile))


async def test_async_timeout_releases_pipes_held_by_escaped_grandchild(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    if not os.path.isdir("/dev/fd"):
        pytest.skip("no /dev/fd to count open descriptors")
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


IGNORE_STOP_AND_HANG = (
    IGNORE_STOP
    + """
import os, sys, time
open(sys.argv[-1], "w").write(str(os.getpid()))
time.sleep(60)
"""
)


async def test_async_cancel_during_timeout_cleanup_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    # the run times out at 0.5s and starts its 2s SIGTERM grace wait; the outer deadline at
    # 1s cancels it mid-cleanup, which must not skip the SIGKILL
    pidfile = tmp_path / "leader.pid"
    qsv = AsyncQsv(fake_qsv(IGNORE_STOP_AND_HANG), kill_grace=2)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(qsv.run("stats", pidfile, timeout=0.5), 1)
        pid = _read_pid(pidfile)
        assert await asyncio.to_thread(_wait_dead, pid, 6), "leader survived cancellation"
    finally:
        _kill(_read_pid(pidfile))


def test_asyncio_run_returning_mid_cleanup_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    # as above, but asyncio.run() returns right after the outer deadline, so any cleanup still
    # running in a background task would be cancelled by the runner's shutdown
    pidfile = tmp_path / "leader.pid"
    qsv = AsyncQsv(fake_qsv(IGNORE_STOP_AND_HANG), kill_grace=2)

    async def main() -> None:
        await asyncio.wait_for(qsv.run("stats", pidfile, timeout=0.5), 1)

    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(main())
        assert _wait_dead(_read_pid(pidfile), 6), "leader survived the runner's shutdown"
    finally:
        _kill(_read_pid(pidfile))


def test_asyncio_run_cancelling_background_run_mid_cleanup_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    # the runner's shutdown cancels the run *and* its cleanup task directly, mid-grace-wait
    pidfile = tmp_path / "leader.pid"
    qsv = AsyncQsv(fake_qsv(IGNORE_STOP_AND_HANG), kill_grace=2)

    async def main() -> None:
        background = asyncio.create_task(qsv.run("stats", pidfile, timeout=0.5))
        await asyncio.sleep(1)
        assert not background.done()  # still in its SIGTERM grace wait

    try:
        asyncio.run(main())
        assert _wait_dead(_read_pid(pidfile), 6), "leader survived the runner's shutdown"
    finally:
        _kill(_read_pid(pidfile))


def _wait_reaped(pid: int, within: float = 5.0) -> bool:
    """Our own child that nothing else will reap: reap it here, so a SIGKILLed one counts."""
    if sys.platform == "win32":  # no zombies (or waitpid) there
        return _wait_dead(pid, within)
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)  # type: ignore[attr-defined,unused-ignore]
        except ChildProcessError:
            return True
        if done:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM")
def test_second_interrupt_during_grace_wait_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path
) -> None:
    # times out at 0.5s and starts its 3s grace wait; a "second Ctrl-C" at 1s interrupts it
    pidfile = tmp_path / "leader.pid"
    qsv = Qsv(fake_qsv(IGNORE_STOP_AND_HANG), kill_grace=3)

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, interrupt)  # type: ignore[attr-defined,unused-ignore]
    signal.setitimer(signal.ITIMER_REAL, 1.0)  # type: ignore[attr-defined,unused-ignore]
    try:
        with pytest.raises(KeyboardInterrupt):
            qsv.run("stats", pidfile, timeout=0.5)
        assert _wait_reaped(_read_pid(pidfile)), "leader survived the second interrupt"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)  # type: ignore[attr-defined,unused-ignore]
        signal.signal(signal.SIGALRM, previous)  # type: ignore[attr-defined,unused-ignore]
        _kill(_read_pid(pidfile))


# 4 MiB, far more than any OS pipe buffer, to a fake that never reads stdin
HANG_WITHOUT_READING = """
import time
time.sleep(60)
"""
BIG_STDIN = b"x" * (4 << 20)


def test_timeout_bounded_when_stdin_is_not_read(fake_qsv: Callable[[str], str]) -> None:
    qsv = Qsv(fake_qsv(HANG_WITHOUT_READING), kill_grace=0.5)
    started = time.monotonic()
    with pytest.raises(QsvTimeout):
        qsv.run("stats", stdin=BIG_STDIN, timeout=1)
    assert time.monotonic() - started < 10


async def test_async_timeout_bounded_when_stdin_is_not_read(
    fake_qsv: Callable[[str], str],
) -> None:
    qsv = AsyncQsv(fake_qsv(HANG_WITHOUT_READING), kill_grace=0.5)
    started = time.monotonic()
    with pytest.raises(QsvTimeout):
        await qsv.run("stats", stdin=BIG_STDIN, timeout=1)
    assert time.monotonic() - started < 10


class _Interrupted(BaseException):
    """Stands in for Ctrl-C (or any BaseException) landing during setup after the spawn."""


def _interrupting_tree(spawned: list[int]) -> type:
    """A setup that is interrupted at once. On Windows qsv is then still suspended (it never
    runs), so record the pid we were handed rather than wait for the fake to write one."""

    class Tree(_process._ProcessTree):
        def __init__(self, pid: int) -> None:
            spawned.append(pid)
            raise _Interrupted

    return Tree


def test_interrupt_during_setup_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[int] = []
    monkeypatch.setattr(_process, "_ProcessTree", _interrupting_tree(spawned))
    try:
        with pytest.raises(_Interrupted):
            Qsv(fake_qsv(IGNORE_STOP_AND_HANG)).run("stats", tmp_path / "leader.pid")
        assert _wait_dead(spawned[0]), "leader survived an interrupted setup"
    finally:
        for pid in spawned:
            _kill(pid)


async def test_async_interrupt_during_setup_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[int] = []
    monkeypatch.setattr(_process, "_ProcessTree", _interrupting_tree(spawned))
    try:
        with pytest.raises(_Interrupted):
            await AsyncQsv(fake_qsv(IGNORE_STOP_AND_HANG)).run("stats", tmp_path / "leader.pid")
        assert await asyncio.to_thread(_wait_dead, spawned[0]), "leader survived"
    finally:
        for pid in spawned:
            _kill(pid)


def test_stdin_feeder_failing_to_start_still_kills(
    fake_qsv: Callable[[str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the Windows stdin feeder, forced on here, cannot get a thread
    pidfile = tmp_path / "leader.pid"

    def no_thread(self: threading.Thread) -> None:
        _read_pid(pidfile)
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(_process, "IS_WINDOWS", True)
    monkeypatch.setattr(threading.Thread, "start", no_thread)
    try:
        with pytest.raises(RuntimeError):
            Qsv(fake_qsv(IGNORE_STOP_AND_HANG), kill_grace=0.5).run(
                "stats", pidfile, stdin=b"a,b\n"
            )
        assert _wait_reaped(_read_pid(pidfile)), "leader survived a failed feeder start"
    finally:
        _kill(_read_pid(pidfile))
