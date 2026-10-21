"""The tray's controller: hosts or attaches to the daemon and keeps :class:`TrayState` current.

Every action, including the poller's refresh, runs on one worker thread, so lifecycle
changes never race each other and the GUI thread never blocks. The tray reaches the daemon
only through :class:`DaemonClient`, whether it hosts the daemon or not.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
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
from .model import Mode, Pending, ServiceKind, TrayState
from .notify import Notifier, nothing

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
        self.notify: Notifier = nothing
        """Shows a desktop notification, on the worker thread."""
        self._announced: set[str] = set()
        self._state = TrayState()
        self._lock = threading.Lock()
        # touched only on the worker thread
        self._host: DaemonHost | None = None
        self._client: DaemonClient | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qsv-tray")
        self._stopping = threading.Event()
        self._poller = threading.Thread(target=self._poll, name="qsv-tray-poll", daemon=True)
        self._actions: Mapping[str, Callable[..., None]] = {
            REFRESH: self._refresh,
            model.START: self._start,
            model.RETRY: self._start,
            model.STOP: self._stop,
            model.RESTART: self._restart,
            model.TAKEOVER: self._takeover,
            model.APPROVE: self._approve,
            model.DENY: self._deny,
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

    def submit(self, action: str, arg: str | None = None) -> Future[None]:
        """Queue an action (a ``model`` action id, or REFRESH) for the worker; APPROVE and
        DENY take the pairing code as ``arg``.

        START at launch attaches to a daemon that is already running, else hosts one.
        """
        handler = self._actions[action]
        args = () if arg is None else (arg,)
        return self._executor.submit(self._run, action, lambda: handler(*args), arg)

    def _run(self, action: str, handler: Callable[[], None], arg: str | None = None) -> None:
        if action != REFRESH:
            log.info("tray action: %s%s", action, f" {arg}" if arg else "")
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

    def _pairings(self, client: DaemonClient, status: Mapping[str, Any]) -> tuple[Pending, ...]:
        """Requests waiting for a decision, fetched only when the status says there are any."""
        count = status.get("pending_pairings")
        if not isinstance(count, int) or count <= 0:
            return ()
        items = client.request("GET", "/v1/pairings", wait=_STATUS_WAIT)
        return model.pending_requests(items)

    def _announce(self, pairings: tuple[Pending, ...]) -> None:
        """Notify once per new request. A code that left the list (decided, or expired) is
        forgotten; it can't come back."""
        codes = {p.text.code for p in pairings}
        for request in pairings:
            t = request.text
            if t.code in self._announced:
                continue
            if not t.can_approve:
                message = f"{t.code}: a schematic that matches no registered dataset"
            elif t.warning:
                message = f"{t.code}: {t.title!r}, from {t.origin} (not a local file)"
            else:
                message = f"{t.code}: {t.title!r}"
            self.notify(
                "Pairing request", message + ". Approve or deny it from the qsv-clientd menu."
            )
        self._announced = codes

    def _attached(self, client: DaemonClient) -> None:
        status = self._status(client)
        pairings = self._pairings(client, status)
        self._client = client
        self._set(
            mode=Mode.ATTACHED,
            status=status,
            pairings=pairings,
            service=self._service_installed(),
            busy=None,
            error=None,
        )
        self._announce(pairings)
        log.info("attached to qsv-clientd (pid %s)", status.get("pid"))

    def _start(self) -> None:
        self._stop_host()  # after an error, the old host may still hold its listeners
        # check again: a daemon may have started since (two would fight over local-token)
        client = self._probe()
        if client is not None:
            self._attached(client)
            return
        self._set(mode=Mode.STARTING, status=None, pairings=(), busy=None, error=None)
        # kept before start(): a start that times out may still finish, and must be stopped
        self._host = host = DaemonHost(self._make_daemon())
        host.start()
        self._client = client = host.client()
        status = self._status(client)
        pairings = self._pairings(client, status)
        self._set(mode=Mode.HOSTED, status=status, pairings=pairings, service=None)
        self._announce(pairings)
        log.info("hosting qsv-clientd")

    def _stop_host(self) -> None:
        host = self._host
        if host is not None:
            self._client = None
            # forgotten only once stopped: after a stop that timed out, Retry and Quit try again
            host.stop()
            self._host = None
            log.info("stopped the hosted qsv-clientd")

    def _stop(self) -> None:
        self._set(busy="stopping…")
        self._stop_host()
        self._set(mode=Mode.STOPPED, status=None, pairings=(), busy=None)

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
            pairings = self._pairings(client, status)
        except QsvDaemonError as exc:
            if state.mode is Mode.HOSTED:
                self._set(mode=Mode.ERROR, error=f"qsv-clientd stopped answering: {exc}")
            else:
                self._client = None
                self._set(mode=Mode.STOPPED, status=None, pairings=(), service=None)
                log.info("the attached qsv-clientd went away")
            return
        # one update for both, so the dot and the menu change together
        if status != state.status or pairings != state.pairings:
            self._set(status=status, pairings=pairings)
        self._announce(pairings)

    def _approve(self, code: str) -> None:
        self._decide(code, "approve")

    def _deny(self, code: str) -> None:
        self._decide(code, "deny")

    def _decide(self, code: str, verb: str) -> None:
        client = self._client
        if client is None:  # the daemon went away since the menu was drawn
            return
        path = f"/v1/pairings/{urllib.parse.quote(code, safe='')}/{verb}"
        try:
            client.request("POST", path, wait=_STATUS_WAIT)
        except QsvDaemonError as exc:
            # the code expired, or its dataset was unregistered: say so, it's no tray error
            if exc.status not in (404, 409):
                raise
            self.notify(f"Couldn't {verb} {code}", str(exc))
        else:
            log.info("pairing %s: %s", "approved" if verb == "approve" else "denied", code)
        self._refresh()
