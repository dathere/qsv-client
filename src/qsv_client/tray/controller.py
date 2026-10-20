"""The tray's controller: hosts or attaches to the daemon and keeps :class:`TrayState` current.

Every action, including the poller's refresh, runs on one worker thread, so lifecycle
changes never race each other and the GUI thread never blocks. The tray reaches the daemon
only through :class:`DaemonClient`, whether it hosts the daemon or not.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from ..daemon import service
from ..daemon.client import DaemonClient
from ..daemon.config import DaemonConfig
from ..daemon.host import DaemonHost
from ..daemon.server import Daemon
from ..errors import QsvDaemonError
from . import model
from .model import Mode, ServiceKind, TrayState

log = logging.getLogger("qsv_client.tray")

REFRESH = "refresh"
_STATUS_WAIT = 5.0


def _load_daemon() -> Daemon:
    # read daemon.json afresh each time, so a restart picks up edits
    return Daemon(DaemonConfig.load())


class Controller:
    def __init__(
        self,
        *,
        make_daemon: Callable[[], Daemon] = _load_daemon,
        attach: Callable[[], DaemonClient] = DaemonClient,
        service_installed: Callable[[], ServiceKind | None] = service.service_installed,
        stop_service: Callable[[], str] = service.stop_service,
        poll_interval: float = 2.0,
        takeover_timeout: float = 30.0,
    ) -> None:
        self._make_daemon = make_daemon
        self._attach = attach
        self._service_installed = service_installed
        self._stop_service = stop_service
        self._poll_interval = poll_interval
        self._takeover_timeout = takeover_timeout
        self.on_change: Callable[[TrayState], None] = lambda state: None
        """Called with each new state, on the worker thread."""
        self._state = TrayState()
        self._lock = threading.Lock()
        # touched only on the worker thread
        self._host: DaemonHost | None = None
        self._client: DaemonClient | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qsv-tray")
        self._stopping = threading.Event()
        self._poller = threading.Thread(target=self._poll, name="qsv-tray-poll", daemon=True)
        self._actions: Mapping[str, Callable[[], None]] = {
            REFRESH: self._refresh,
            model.START: self._start,
            model.RETRY: self._start,
            model.STOP: self._stop,
            model.RESTART: self._restart,
            model.TAKEOVER: self._takeover,
        }

    @property
    def state(self) -> TrayState:
        with self._lock:
            return self._state

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state = state = replace(self._state, **changes)
        try:
            self.on_change(state)
        except Exception:
            log.exception("updating the tray failed")

    # -- running actions ---------------------------------------------------------------------

    def submit(self, action: str) -> Future[None]:
        """Queue an action (a ``model`` action id, or REFRESH) for the worker.

        START at launch attaches to a daemon that is already running, else hosts one.
        """
        return self._executor.submit(self._run, action, self._actions[action])

    def _run(self, action: str, handler: Callable[[], None]) -> None:
        if action != REFRESH:
            log.info("tray action: %s", action)
        try:
            handler()
        except Exception as exc:
            log.exception("%s failed", action)
            # keep the host, if any: Stop, Restart and Quit still clean it up
            self._set(mode=Mode.ERROR, busy=None, error=str(exc) or type(exc).__name__)

    def start_polling(self) -> None:
        self._poller.start()

    def _poll(self) -> None:
        pending: Future[None] | None = None
        while not self._stopping.wait(self._poll_interval):
            # one refresh at a time: none piles up behind a long action
            # (shutdown() joins this thread before shutting the executor down)
            if pending is None or pending.done():
                pending = self.submit(REFRESH)

    def shutdown(self) -> None:
        """Stop polling, finish the running action, then stop a daemon the tray hosts."""
        self._stopping.set()
        if self._poller.ident is not None:
            self._poller.join()
        self._executor.shutdown(wait=True, cancel_futures=True)
        try:
            self._stop_host()
        except Exception:
            log.exception("stopping qsv-clientd failed")

    # -- actions (worker thread) -------------------------------------------------------------

    def _probe(self) -> DaemonClient | None:
        """A client of a daemon that is already running, if one answers."""
        try:
            client = self._attach()
        except QsvDaemonError:  # on Windows: no local-token file, so none running
            return None
        return client if client.available() else None

    def _status(self, client: DaemonClient) -> dict[str, Any]:
        status: dict[str, Any] = client.request("GET", "/v1/status", wait=_STATUS_WAIT)
        return status

    def _attached(self, client: DaemonClient) -> None:
        status = self._status(client)
        self._client = client
        self._set(
            mode=Mode.ATTACHED,
            status=status,
            service=self._service_installed(),
            busy=None,
            error=None,
        )
        log.info("attached to qsv-clientd (pid %s)", status.get("pid"))

    def _start(self) -> None:
        self._stop_host()  # after an error, the old host may still hold its listeners
        # check again: a daemon may have started since (two would fight over local-token)
        client = self._probe()
        if client is not None:
            self._attached(client)
            return
        self._set(mode=Mode.STARTING, status=None, busy=None, error=None)
        host = DaemonHost(self._make_daemon())
        host.start()
        self._host = host
        self._client = host.client()
        self._set(mode=Mode.HOSTED, status=self._status(self._client), service=None)
        log.info("hosting qsv-clientd")

    def _stop_host(self) -> None:
        host, self._host = self._host, None
        if host is not None:
            self._client = None
            host.stop()
            log.info("stopped the hosted qsv-clientd")

    def _stop(self) -> None:
        self._set(busy="stopping…")
        self._stop_host()
        self._set(mode=Mode.STOPPED, status=None, busy=None)

    def _restart(self) -> None:
        self._set(busy="restarting…")
        self._stop_host()
        self._start()

    def _takeover(self) -> None:
        self._set(busy="stopping the service…")
        log.info(self._stop_service())
        client = self._client
        deadline = time.monotonic() + self._takeover_timeout
        while client is not None and client.available():
            if time.monotonic() >= deadline:
                raise RuntimeError("the service is still running after it was stopped")
            time.sleep(0.2)
        self._client = None
        self._start()

    def _refresh(self) -> None:
        state = self.state
        if state.busy:
            return
        if state.mode is Mode.STOPPED:
            # a daemon started elsewhere (a service, `qsv-clientd serve`): watch it
            client = self._probe()
            if client is not None:
                self._attached(client)
            return
        client = self._client
        if client is None or state.mode not in (Mode.HOSTED, Mode.ATTACHED):
            return
        try:
            status = self._status(client)
        except QsvDaemonError as exc:
            if state.mode is Mode.HOSTED:
                self._set(mode=Mode.ERROR, error=f"qsv-clientd stopped answering: {exc}")
            else:
                self._client = None
                self._set(mode=Mode.STOPPED, status=None, service=None)
                log.info("the attached qsv-clientd went away")
            return
        if status != state.status:
            self._set(status=status)
