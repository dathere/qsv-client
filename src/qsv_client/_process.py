"""Spawn qsv and reap it, including any children it started, on timeout or cancellation.

qsv commands can start their own children (``validate`` runs pyshacl, ``viz`` a webdriver,
``describegpt`` may shell out). Killing only the qsv pid would orphan them, so each run's
process tree is tracked and stopped as a whole: first politely, then forcibly after a grace
period.

- POSIX: each run gets its own session (process group). SIGTERM, then SIGKILL, to the group.
  A descendant that leaves the group (e.g. by calling ``setsid``) survives the kill and may
  keep the output pipes open; after a timeout, output is collected for at most ``kill_grace``
  seconds and anything that descendant still holds is dropped.
- Windows: each run is put in its own Job Object, which every process it starts joins and
  cannot leave. ``CTRL_BREAK_EVENT`` to its process group, then ``TerminateJobObject``. A
  child started in the instant between spawning qsv and assigning it to the job is missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, TypeVar

IS_WINDOWS = sys.platform == "win32"

_T = TypeVar("_T")


@dataclass(frozen=True)
class RawResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    def _spawn_kwargs() -> dict[str, Any]:
        # its own console process group, so CTRL_BREAK_EVENT reaches qsv and its children only
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def _create_job(pid: int) -> int | None:
        """A Job Object holding ``pid`` (and, from now on, everything it starts), or None."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        try:
            if handle and _kernel32.AssignProcessToJobObject(job, handle):
                return int(job)
        finally:
            if handle:
                _kernel32.CloseHandle(handle)
        _kernel32.CloseHandle(job)
        return None

    class _ProcessTree:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self._job = _create_job(pid)

        def interrupt(self) -> None:
            with contextlib.suppress(OSError):
                os.kill(self.pid, signal.CTRL_BREAK_EVENT)

        def kill(self) -> None:
            if self._job is not None and _kernel32.TerminateJobObject(self._job, 1):
                return
            # no job (creation failed): the leader is all we can reach. os.kill on Windows
            # is TerminateProcess for anything but the console events.
            with contextlib.suppress(OSError):
                os.kill(self.pid, signal.SIGTERM)

        def close(self) -> None:
            if self._job is not None:
                _kernel32.CloseHandle(self._job)
                self._job = None

else:

    def _spawn_kwargs() -> dict[str, Any]:
        return {"start_new_session": True}

    def _signal_group(pid: int, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)

    class _ProcessTree:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def interrupt(self) -> None:
            _signal_group(self.pid, signal.SIGTERM)

        def kill(self) -> None:
            # the whole group, even if the leader already exited: its children may not have
            _signal_group(self.pid, signal.SIGKILL)

        def close(self) -> None:
            pass


def _terminate_sync(proc: subprocess.Popen[bytes], tree: _ProcessTree, grace: float) -> None:
    tree.interrupt()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    tree.kill()


def run_sync(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    stdin: bytes | None,
    stdout_file: IO[bytes] | None,
    timeout: float | None,
    kill_grace: float,
) -> RawResult:
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=stdout_file if stdout_file is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        **_spawn_kwargs(),
    )
    tree = _ProcessTree(proc.pid)
    timed_out = False
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_sync(proc, tree, kill_grace)
        try:
            out, err = proc.communicate(timeout=kill_grace)
        except subprocess.TimeoutExpired:
            # a descendant that left the group still holds the pipes: drop its output
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            proc.wait()
            out, err = b"", b""
    except BaseException:
        # KeyboardInterrupt etc.: never leave a qsv process tree running behind us
        _terminate_sync(proc, tree, kill_grace)
        proc.wait()
        raise
    finally:
        tree.close()
    return RawResult(
        exit_code=proc.returncode,
        stdout=out or b"",
        stderr=err or b"",
        timed_out=timed_out,
    )


async def _terminate_async(
    proc: asyncio.subprocess.Process, tree: _ProcessTree, grace: float
) -> None:
    # proc.wait() also waits for the output pipes to close, which a descendant that left the
    # group can hold open indefinitely, so every wait here is bounded
    tree.interrupt()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), grace)
    tree.kill()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), grace)


async def _release_async(proc: asyncio.subprocess.Process) -> None:
    """Close our ends of the pipes, then reap the (already killed) leader.

    Without the close, pipes held by an escaped descendant would keep their fds open and
    ``wait()`` pending until it exits. asyncio has no public API for this.
    """
    _close_pipes(proc)
    await proc.wait()


def _close_pipes(proc: asyncio.subprocess.Process) -> None:
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()


def _kill_now(proc: asyncio.subprocess.Process, tree: _ProcessTree) -> None:
    """Teardown was itself cancelled (e.g. by ``asyncio.run()`` shutting down): skip the
    remaining grace and do the parts that matter synchronously."""
    tree.kill()
    _close_pipes(proc)


async def _reap_async(proc: asyncio.subprocess.Process, tree: _ProcessTree, grace: float) -> None:
    try:
        await _terminate_async(proc, tree, grace)
        await _release_async(proc)
    except asyncio.CancelledError:
        _kill_now(proc, tree)
        raise


async def _timeout_cleanup(
    proc: asyncio.subprocess.Process, tree: _ProcessTree, grace: float
) -> bytes:
    """Kill and release a timed-out run; return whatever stderr arrives within ``grace``."""
    try:
        await _terminate_async(proc, tree, grace)
        err = b""
        if proc.stderr is not None:
            with contextlib.suppress(Exception):
                err = await asyncio.wait_for(proc.stderr.read(), grace)
        await _release_async(proc)
    except asyncio.CancelledError:
        _kill_now(proc, tree)
        raise
    return err


async def _finish_despite_cancel(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run ``coro`` to completion even if we are cancelled meanwhile, then re-raise the cancel.

    ``asyncio.shield`` alone would propagate the cancellation at once, leaving the cleanup in
    a background task that an ``asyncio.run()`` returning right after would cancel, skipping
    the forced kill.
    """
    task = asyncio.ensure_future(coro)
    cancel: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            cancel = exc
    if cancel is not None:
        raise cancel
    return task.result()


async def run_async(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    stdin: bytes | None,
    stdout_file: IO[bytes] | None,
    timeout: float | None,
    kill_grace: float,
) -> RawResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=stdout_file if stdout_file is not None else asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        **_spawn_kwargs(),
    )
    tree = _ProcessTree(proc.pid)
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        # a cancellation arriving mid-cleanup must not skip the forced kill or the release
        out, err = b"", await _finish_despite_cancel(_timeout_cleanup(proc, tree, kill_grace))
    except BaseException:
        # task cancelled (or the loop is shutting down): reap the tree, then propagate
        await _finish_despite_cancel(_reap_async(proc, tree, kill_grace))
        raise
    finally:
        tree.close()
    assert proc.returncode is not None
    return RawResult(
        exit_code=proc.returncode,
        stdout=out or b"",
        stderr=err or b"",
        timed_out=timed_out,
    )
