"""TEMPORARY diagnostics (diag branch only, never merged): why a killed leader "survives"
asyncio.run() shutdown on macOS / Python 3.14 in CI.

Load with: PYTHONPATH=tests pytest -p _teardown_diag ...

Records every killpg() the client sends and every forced-kill path it takes, and when a
test's _wait_dead() returns False, prints that trace plus `ps` for the leader and its group.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from qsv_client import _process

EVENTS: list[str] = []
T0 = time.monotonic()


def _log(msg: str) -> None:
    EVENTS.append(f"+{time.monotonic() - T0:8.3f}s [{threading.current_thread().name}] {msg}")


def _ps(pid: int) -> str:
    out = subprocess.run(
        ["ps", "-A", "-o", "pid,ppid,pgid,stat,etime,wchan,command"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    try:
        pgid = os.getpgid(pid)
    except OSError as exc:
        pgid = None
        out.insert(1, f"(getpgid({pid}): {exc!r})")
    keep = [out[0]]
    for line in out[1:]:
        cols = line.split()
        if len(cols) > 2 and (cols[0] == str(pid) or cols[2] == str(pgid) or cols[1] == str(pid)):
            keep.append(line)
    return "\n".join(keep)


@pytest.fixture(autouse=True)
def _trace(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    EVENTS.clear()
    _log(f"test start, python {sys.version.split()[0]}, me {os.getpid()}")

    def signal_group(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except OSError as exc:
            _log(f"killpg({pid}, {sig}) -> {type(exc).__name__} errno={exc.errno}")
        else:
            _log(f"killpg({pid}, {sig}) ok")

    monkeypatch.setattr(_process, "_signal_group", signal_group)

    real_kill_now = _process._kill_now

    def kill_now(proc: Any, tree: Any) -> None:
        _log(f"_kill_now(pid={proc.pid}, returncode={proc.returncode})")
        real_kill_now(proc, tree)

    monkeypatch.setattr(_process, "_kill_now", kill_now)

    real_reap = _process._reap_async

    async def reap(proc: Any, tree: Any, grace: float) -> None:
        _log(f"_reap_async start pid={proc.pid} grace={grace}")
        try:
            await real_reap(proc, tree, grace)
        except BaseException as exc:
            _log(f"_reap_async ended by {type(exc).__name__} rc={proc.returncode}")
            raise
        _log(f"_reap_async done rc={proc.returncode}")

    monkeypatch.setattr(_process, "_reap_async", reap)

    real_timeout_cleanup = _process._timeout_cleanup

    async def timeout_cleanup(proc: Any, *args: Any) -> Any:
        _log(f"_timeout_cleanup start pid={proc.pid}")
        try:
            return await real_timeout_cleanup(proc, *args)
        except BaseException as exc:
            _log(f"_timeout_cleanup ended by {type(exc).__name__} rc={proc.returncode}")
            raise
        finally:
            _log(f"_timeout_cleanup exit rc={proc.returncode}")

    monkeypatch.setattr(_process, "_timeout_cleanup", timeout_cleanup)

    for name in ("test_process", "test_core_edges"):
        module = sys.modules.get(name)
        if module is None:
            continue
        real_wait_dead = module._wait_dead

        def wait_dead(pid: int, within: float = 5.0, _real: Any = real_wait_dead) -> bool:
            _log(f"_wait_dead({pid}, {within}) begins")
            dead = bool(_real(pid, within))
            _log(f"_wait_dead({pid}) -> {dead}")
            if not dead:
                print("\n===== DIAG: leader not dead =====", file=sys.stderr)
                print(f"load average: {os.getloadavg()}", file=sys.stderr)
                print("\n".join(EVENTS), file=sys.stderr)
                print(_ps(pid), file=sys.stderr)
                time.sleep(5)
                print("--- 5s later:", file=sys.stderr)
                print(_ps(pid), file=sys.stderr)
                print("===== END DIAG =====", file=sys.stderr)
            return dead

        monkeypatch.setattr(module, "_wait_dead", wait_dead)
    yield


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" and (report.failed or os.environ.get("DIAG_ALWAYS")):
        print("\n===== DIAG: failed test trace =====", file=sys.stderr)
        print("\n".join(EVENTS), file=sys.stderr)
        print("===== END =====", file=sys.stderr)
