"""Spawn qsv and reap it, including any children it started, on timeout or cancellation.

qsv commands can start their own children (``validate`` runs pyshacl, ``viz`` a webdriver,
``describegpt`` may shell out). Killing only the qsv pid would orphan them, so on POSIX each
run gets its own session (process group) and the whole group is signalled: SIGTERM first,
then SIGKILL after a grace period. On Windows the process is started in a new process group
and killed directly; grandchildren are not reaped there.

A descendant that leaves the group (e.g. by calling ``setsid``) survives the kill and may keep
the output pipes open; after a timeout, output is collected for at most ``kill_grace`` seconds
and anything that descendant still holds is dropped.
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


def _spawn_kwargs() -> dict[str, Any]:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined,unused-ignore]
    return {"start_new_session": True}


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)  # type: ignore[attr-defined,unused-ignore]


def _terminate_sync(proc: subprocess.Popen[bytes], grace: float) -> None:
    if IS_WINDOWS:
        with contextlib.suppress(OSError):
            proc.kill()
        return
    _signal_group(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    # SIGKILL the group even if the leader already exited: its children may not have
    _signal_group(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]


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
    timed_out = False
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_sync(proc, kill_grace)
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
        # KeyboardInterrupt etc.: never leave a qsv process group running behind us
        _terminate_sync(proc, kill_grace)
        proc.wait()
        raise
    return RawResult(
        exit_code=proc.returncode,
        stdout=out or b"",
        stderr=err or b"",
        timed_out=timed_out,
    )


async def _terminate_async(proc: asyncio.subprocess.Process, grace: float) -> None:
    if IS_WINDOWS:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), grace)
        return
    # proc.wait() also waits for the output pipes to close, which a descendant that left the
    # group can hold open indefinitely, so every wait here is bounded
    _signal_group(proc.pid, signal.SIGTERM)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), grace)
    _signal_group(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), grace)


async def _release_async(proc: asyncio.subprocess.Process) -> None:
    """Close our ends of the pipes, then reap the (already killed) leader.

    Without the close, pipes held by an escaped descendant would keep their fds open and
    ``wait()`` pending until it exits. asyncio has no public API for this.
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()
    await proc.wait()


async def _reap_async(proc: asyncio.subprocess.Process, grace: float) -> None:
    await _terminate_async(proc, grace)
    await _release_async(proc)


async def _timeout_cleanup(proc: asyncio.subprocess.Process, grace: float) -> bytes:
    """Kill and release a timed-out run; return whatever stderr arrives within ``grace``."""
    await _terminate_async(proc, grace)
    err = b""
    if proc.stderr is not None:
        with contextlib.suppress(Exception):
            err = await asyncio.wait_for(proc.stderr.read(), grace)
    await _release_async(proc)
    return err


async def _finish_despite_cancel(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run ``coro`` to completion even if we are cancelled meanwhile, then re-raise the cancel.

    ``asyncio.shield`` alone would propagate the cancellation at once, leaving the cleanup in
    a background task that an ``asyncio.run()`` returning right after would cancel, skipping
    the SIGKILL.
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
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        # a cancellation arriving mid-cleanup must not skip the SIGKILL or the release
        out, err = b"", await _finish_despite_cancel(_timeout_cleanup(proc, kill_grace))
    except BaseException:
        # task cancelled (or the loop is shutting down): reap the group, then propagate
        await _finish_despite_cancel(_reap_async(proc, kill_grace))
        raise
    assert proc.returncode is not None
    return RawResult(
        exit_code=proc.returncode,
        stdout=out or b"",
        stderr=err or b"",
        timed_out=timed_out,
    )
