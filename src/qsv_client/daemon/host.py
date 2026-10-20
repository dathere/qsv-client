"""Run a :class:`~qsv_client.daemon.server.Daemon` on a background thread.

``Daemon.serve_forever`` installs signal handlers, which only work on the main thread; a GUI
(the tray) needs the main thread for itself, so it hosts the daemon here instead. A host
starts once: to restart, stop it and build a new ``Daemon`` and ``DaemonHost``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading

from .client import DaemonClient
from .server import Daemon

_START_TIMEOUT = 60.0
_STOP_TIMEOUT = 30.0
"""Seconds to wait for the daemon to close, on top of twice its kill_grace."""


class DaemonHost:
    """A :class:`Daemon` serving from its own event loop, so sync clients can call it."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._main, name="qsv-clientd", daemon=True)

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            try:
                self.loop.run_until_complete(self.daemon.start())
            except BaseException as exc:
                self._error = exc
                # a start that failed after binding still holds its listeners
                with contextlib.suppress(BaseException):
                    self.loop.run_until_complete(self.daemon.close())
                self._ready.set()
                return
            # ready only once the loop runs, so a stop() right after start() finds it running
            self.loop.call_soon(self._ready.set)
            self.loop.run_forever()
        finally:
            self._close_loop()

    def _close_loop(self) -> None:
        """Finish what is left on the loop, then close it (else: "unclosed event loop")."""
        loop = self.loop
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        asyncio.set_event_loop(None)
        loop.close()

    def start(self) -> DaemonHost:
        """Start serving; raises whatever ``Daemon.start`` raised (no qsv, port in use...)."""
        self._thread.start()
        if not self._ready.wait(_START_TIMEOUT):
            raise TimeoutError(f"qsv-clientd did not start within {_START_TIMEOUT:.0f}s")
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        """Close the daemon (removing its socket, local-token and daemon.json) and the loop.

        Closing kills running qsv processes, which can take their ``kill_grace``.
        """
        if self._thread.ident is None:  # never started: the loop never ran
            self.loop.close()
            return
        done = None
        if self._error is None and self.loop.is_running():
            done = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        # killing a run's process tree can wait kill_grace after SIGTERM and again after SIGKILL
        timeout = _STOP_TIMEOUT + 2 * self.daemon.config.kill_grace
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError(f"qsv-clientd did not stop within {timeout:.0f}s")
        if done is not None:
            done.result()  # close()'s own error, if any

    async def _shutdown(self) -> None:
        # the loop stops however long or badly close() goes, so the thread always ends
        try:
            await self.daemon.close()
        finally:
            self.loop.stop()

    @property
    def port(self) -> int:
        assert self.daemon.port is not None
        return self.daemon.port

    def client(self) -> DaemonClient:
        """A local client of this daemon: its socket, or on Windows its port and local token."""
        if sys.platform != "win32":
            return DaemonClient(socket_path=self.daemon.socket_path)
        return DaemonClient(port=self.port, token=self.daemon.local_token)
