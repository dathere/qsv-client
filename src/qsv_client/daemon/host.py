"""Run a :class:`~qsv_client.daemon.server.Daemon` on a background thread.

``Daemon.serve_forever`` installs signal handlers, which only work on the main thread; a GUI
(the tray) needs the main thread for itself, so it hosts the daemon here instead. A host
starts once: to restart, stop it and build a new ``Daemon`` and ``DaemonHost``.
"""

from __future__ import annotations

import asyncio
import sys
import threading

from .client import DaemonClient
from .server import Daemon


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
                return
            finally:
                self._ready.set()
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
        assert self._ready.wait(60), "daemon did not start"
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        """Close the daemon (removing its socket, local-token and daemon.json) and the loop."""
        if self._thread.ident is None:  # never started: the loop never ran
            self.loop.close()
            return
        if self._error is None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.daemon.close(), self.loop).result(30)
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(30)

    @property
    def port(self) -> int:
        assert self.daemon.port is not None
        return self.daemon.port

    def client(self) -> DaemonClient:
        """A local client of this daemon: its socket, or on Windows its port and local token."""
        if sys.platform != "win32":
            return DaemonClient(socket_path=self.daemon.socket_path)
        return DaemonClient(port=self.port, token=self.daemon.local_token)
