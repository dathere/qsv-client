"""The tray's controller: hosts or attaches to the daemon and keeps :class:`TrayState` current.

Every action, including the poller's refresh, runs on one worker thread, so lifecycle
changes never race each other and the GUI thread never blocks. The tray reaches the daemon
only through :class:`DaemonClient`, whether it hosts the daemon or not.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..daemon import service
from ..daemon.client import DaemonClient
from ..daemon.config import DaemonConfig, config_dir, log_path
from ..daemon.host import DaemonHost
from ..daemon.server import Daemon
from ..errors import QsvDaemonError
from . import _open, autostart, model
from .model import DatasetView, Job, Mode, Pending, ServiceKind, TrayState
from .notify import Notifier, nothing

log = logging.getLogger("qsv_client.tray")

REFRESH = "refresh"
_PAIRING_ACTIONS = frozenset({model.APPROVE, model.DENY, model.WRONG_CODE})
_STATUS_WAIT = 5.0


def load_daemon(binary: str | None = None) -> Daemon:
    """A daemon from daemon.json, read afresh each time so a restart picks up edits.
    ``binary`` overrides its ``binary``, as `qsv-clientd serve --binary` does."""
    config = DaemonConfig.load()
    if binary is not None:
        config.binary = binary
    return Daemon(config)


class Controller:
    def __init__(
        self,
        *,
        make_daemon: Callable[[], Daemon] = load_daemon,
        attach: Callable[[], DaemonClient] = DaemonClient,
        service_installed: Callable[[], ServiceKind | None] = service.service_installed,
        stop_service: Callable[[], str] = service.stop_service,
        poll_interval: float = 2.0,
        takeover_timeout: float = 30.0,
        open_path: Callable[[Path], None] = _open.open_path,
        autostart_enabled: Callable[[], bool | None] = autostart.is_enabled,
        enable_autostart: Callable[[], str] = autostart.enable,
        disable_autostart: Callable[[], str] = autostart.disable,
    ) -> None:
        self._make_daemon = make_daemon
        self._attach = attach
        self._service_installed = service_installed
        self._stop_service = stop_service
        self._poll_interval = poll_interval
        self._takeover_timeout = takeover_timeout
        self._open_path = open_path
        self._autostart_enabled = autostart_enabled
        self._enable_autostart = enable_autostart
        self._disable_autostart = disable_autostart
        self.on_change: Callable[[TrayState], None] = lambda state: None
        """Called with each new state, on the worker thread."""
        self.notify: Notifier = nothing
        """Shows a desktop notification, on the worker thread."""
        self._announced: set[str] = set()
        self._state = TrayState()
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
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
            model.WRONG_CODE: self._wrong_code,
            model.OPEN_SCHEMATIC: self._open_schematic,
            model.RENDER: lambda dataset_id: self._start_job(model.RENDER, dataset_id),
            model.FETCH: lambda dataset_id: self._start_job(model.FETCH, dataset_id),
            model.FETCH_FORCE: lambda dataset_id: self._start_job(model.FETCH_FORCE, dataset_id),
            model.FORGET: self._forget,
            model.REVOKE_ALL: self._revoke_all,
            model.OPEN_CONFIG: self._open_config,
            model.OPEN_LOG: self._open_log,
            model.AUTOSTART: self._toggle_autostart,
        }

    @property
    def state(self) -> TrayState:
        with self._lock:
            return self._state

    def _set(self, **changes: Any) -> None:
        self._update(lambda state: replace(state, **changes))

    def _update(self, change: Callable[[TrayState], TrayState]) -> None:
        """Apply ``change`` to the current state atomically: job threads add and remove
        their jobs while the worker refreshes."""
        with self._lock:
            self._state = change(self._state)
        # one at a time, and always the latest: two threads changing it together must not
        # leave the GUI showing the older state (a finished job still running, say)
        with self._emit_lock:
            try:
                self.on_change(self.state)
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
            # a pairing action's argument is the code, which no log may hold (see _announce)
            shown = arg if arg and action not in _PAIRING_ACTIONS else None
            log.info("tray action: %s%s", action, f" {shown}" if shown else "")
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
        forgotten; it can't come back. The code itself is never shown: approving means
        recognizing it, from the page, among decoys."""
        codes = {p.text.code for p in pairings}
        for request in pairings:
            t = request.text
            if t.code in self._announced:
                continue
            if not t.can_approve:
                self.notify(
                    "Pairing request",
                    "A schematic that matches no registered dataset wants to pair. "
                    "Deny it from the qsv-clientd menu, or register the dataset first.",
                )
                continue
            source = f", from {t.origin} (not a local file)" if t.warning else ""
            self.notify(
                "Pairing request",
                f"{t.title!r} wants to pair{source}. To approve, pick the code the page shows "
                "in the qsv-clientd menu.",
            )
        self._announced = codes

    def _attached(self, client: DaemonClient) -> None:
        status = self._status(client)
        pairings = self._pairings(client, status)
        datasets = self._datasets(client, status)
        self._client = client
        self._set(
            mode=Mode.ATTACHED,
            status=status,
            pairings=pairings,
            datasets=datasets,
            service=self._service_installed(),
            busy=None,
            error=None,
        )
        self._announce(pairings)
        log.info("attached to qsv-clientd (pid %s)", status.get("pid"))

    def _start(self) -> None:
        self._read_autostart()
        self._stop_host()  # after an error, the old host may still hold its listeners
        # check again: a daemon may have started since (two would fight over local-token)
        client = self._probe()
        if client is not None:
            self._attached(client)
            return
        self._set(mode=Mode.STARTING, status=None, pairings=(), datasets=(), busy=None, error=None)
        # kept before start(): a start that times out may still finish, and must be stopped
        self._host = host = DaemonHost(self._make_daemon())
        host.start()
        self._client = client = host.client()
        status = self._status(client)
        pairings = self._pairings(client, status)
        datasets = self._datasets(client, status)
        self._set(
            mode=Mode.HOSTED, status=status, pairings=pairings, datasets=datasets, service=None
        )
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
        self._set(mode=Mode.STOPPED, status=None, pairings=(), datasets=(), busy=None)

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
            datasets = self._datasets(client, status)
        except QsvDaemonError as exc:
            if state.mode is Mode.HOSTED:
                self._set(mode=Mode.ERROR, error=f"qsv-clientd stopped answering: {exc}")
            else:
                self._client = None
                self._set(mode=Mode.STOPPED, status=None, pairings=(), datasets=(), service=None)
                log.info("the attached qsv-clientd went away")
            return
        # one update for all, so the dot and the menu change together
        if (status, pairings, datasets) != (state.status, state.pairings, state.datasets):
            ids = {ds.id for ds in datasets}
            self._update(
                lambda s: replace(
                    s,
                    status=status,
                    pairings=pairings,
                    datasets=datasets,
                    force_fetch=frozenset(i for i in s.force_fetch if i in ids),
                )
            )
        self._announce(pairings)

    def _approve(self, code: str) -> None:
        self._decide(code, "approve")

    def _deny(self, code: str) -> None:
        self._decide(code, "deny")

    def _wrong_code(self, code: str) -> None:
        # the picked code isn't this request's: the page the user is looking at didn't make it
        log.warning("a decoy code was picked for a pairing request; denying it")
        if self._decide(code, "deny"):
            self.notify(
                "Pairing denied",
                "The code you picked isn't that request's, so it was denied. If the page shows "
                "that code, its request is another one in the menu; if you misclicked, pair "
                "again from the page.",
            )

    def _decide(self, code: str, verb: str) -> bool:
        """Approve or deny ``code``; False when it no longer could be (already reported)."""
        client = self._client
        if client is None:  # the daemon went away since the menu was drawn
            return False
        path = f"/v1/pairings/{urllib.parse.quote(code, safe='')}/{verb}"
        try:
            client.request("POST", path, wait=_STATUS_WAIT)
        except QsvDaemonError as exc:
            # the code expired, or its dataset was unregistered: say so, it's no tray error
            if exc.status not in (404, 409):
                raise
            # no code: a notifier that fails logs its arguments, and no log may hold one
            self.notify(f"Couldn't {verb} the pairing request", str(exc))
            self._refresh()
            return False
        log.info("pairing request %s", "approved" if verb == "approve" else "denied")
        self._refresh()
        return True

    # -- datasets ------------------------------------------------------------------------------

    def _datasets(self, client: DaemonClient, status: Mapping[str, Any]) -> tuple[DatasetView, ...]:
        """Registered datasets, fetched only when the status says there are any."""
        count = status.get("datasets")
        if not isinstance(count, int) or count <= 0:
            return ()
        return model.dataset_views(client.request("GET", "/v1/datasets", wait=_STATUS_WAIT))

    def _dataset(self, dataset_id: str) -> DatasetView | None:
        return next((ds for ds in self.state.datasets if ds.id == dataset_id), None)

    def _open_schematic(self, dataset_id: str) -> None:
        ds = self._dataset(dataset_id)
        if ds is None or ds.schematic_path is None:
            return
        if not webbrowser.open(Path(ds.schematic_path).as_uri()):
            self.notify(
                f"Couldn't open {ds.title!r}", f"No browser could open {ds.schematic_path}."
            )

    def _forget(self, dataset_id: str) -> None:
        ds, client = self._dataset(dataset_id), self._client
        if ds is None or client is None:
            return
        try:
            reply = client.request("DELETE", _dataset_path(ds.id), wait=_STATUS_WAIT)
        except QsvDaemonError as exc:
            if exc.status != 404:
                raise
            self.notify(f"Couldn't forget {ds.title!r}", str(exc))
        else:
            revoked = reply.get("revoked", 0)
            self.notify(
                f"Forgot {ds.title!r}",
                f"It is no longer registered; {_tokens(revoked)} revoked.",
            )
        self._refresh()

    def _revoke_all(self) -> None:
        client = self._client
        if client is None:
            return
        reply = client.request("POST", "/v1/tokens/revoke", {}, wait=_STATUS_WAIT)
        self.notify(
            "Tokens revoked",
            f"{_tokens(reply.get('revoked', 0))} revoked; open pages must pair again.",
        )
        self._refresh()

    def _start_job(self, kind: str, dataset_id: str) -> None:
        """Run a render or fetch on its own daemon thread: they can take many minutes, which
        must hold up neither the menu nor Quit (a daemon thread doesn't delay exit)."""
        ds, client = self._dataset(dataset_id), self._client
        if ds is None or client is None or any(j.dataset_id == ds.id for j in self.state.jobs):
            return
        verb = "rendering" if kind == model.RENDER else "fetching"
        job = Job(ds.id, f"{verb} {ds.title!r}…")
        self._update(lambda s: replace(s, jobs=(*s.jobs, job)))
        threading.Thread(
            target=self._job, args=(kind, ds, client, job), name=f"qsv-tray-{kind}", daemon=True
        ).start()

    def _job(self, kind: str, ds: DatasetView, client: DaemonClient, job: Job) -> None:
        action = "render" if kind == model.RENDER else "fetch"
        try:
            if kind == model.RENDER:
                reply = client.request("POST", f"{_dataset_path(ds.id)}/render", {}, wait=None)
                title = f"Re-rendered {ds.title!r}"
                message = f"Its schematic was rewritten ({reply.get('bytes', 0):,} bytes)."
            else:
                body = {"force": True} if kind == model.FETCH_FORCE else {}
                reply = client.request("POST", f"{_dataset_path(ds.id)}/fetch", body, wait=None)
                title = f"Fetched {ds.title!r}"
                message = (
                    "The latest data was downloaded and is now in use."
                    if reply.get("changed")
                    else "It was already up to date."
                )
                self._update(lambda s: replace(s, force_fetch=s.force_fetch - {ds.id}))
        except QsvDaemonError as exc:
            if self._stopping.is_set():  # Quit stopped the daemon under it: nothing to report
                return
            title, message = f"Couldn't {action} {ds.title!r}", str(exc)
            if exc.code == "columns_missing":
                self._update(lambda s: replace(s, force_fetch=s.force_fetch | {ds.id}))
                message += (
                    " If that's expected, use Fetch latest data, accepting missing columns, in "
                    "its menu."
                )
        except Exception:
            log.exception("%s of %s failed", action, ds.id)
            title, message = f"Couldn't {action} {ds.title!r}", "See the tray's log."
        finally:
            self._update(lambda s: replace(s, jobs=tuple(j for j in s.jobs if j != job)))
        if self._stopping.is_set():
            return
        self.notify(title, message)
        with contextlib.suppress(RuntimeError):  # Quit shut the worker down meanwhile
            self.submit(REFRESH)

    # -- files and login ---------------------------------------------------------------------

    def _open(self, path: Path) -> bool:
        try:
            self._open_path(path)
        except OSError as exc:
            self.notify(f"Couldn't open {path.name}", str(exc))
            return False
        return True

    def _open_config(self) -> None:
        """Open the default daemon.json, creating it as ``{}`` (the only safe seed: unknown
        keys are errors) so there is something to edit."""
        path = config_dir() / "daemon.json"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
        except OSError as exc:  # a read-only config directory: no reason to show an error state
            self.notify("Couldn't create daemon.json", str(exc))
            return
        if not self._open(path):
            return
        state = self.state
        if state.mode is Mode.ATTACHED and state.service:
            hint = _APPLY_TO_SERVICE.format(state.service)
        else:
            hint = _APPLY_CONFIG.get(state.mode, _APPLY_ON_START)
        self.notify("Editing daemon.json", hint)

    def _open_log(self) -> None:
        state = self.state
        hosted_or_tray = not (state.mode is Mode.ATTACHED and state.service == "launchd")
        path = log_path() if hosted_or_tray else service.launchd_log_path()
        if not path.exists():
            self.notify("No log yet", f"Nothing has been logged to {path} yet.")
            return
        self._open(path)

    def _read_autostart(self) -> None:
        try:
            enabled = self._autostart_enabled()
        except OSError:
            log.warning("couldn't tell whether the tray starts at login", exc_info=True)
            enabled = None
        if enabled != self.state.autostart:
            self._set(autostart=enabled)

    def _toggle_autostart(self) -> None:
        # a login item that couldn't be written is no reason to show the daemon as broken
        try:
            if self.state.autostart:
                log.info(self._disable_autostart())
            else:
                log.info(self._enable_autostart())
        except Exception as exc:  # no qsv to pin, a bad daemon.json, a permission error
            log.warning("changing Start at login failed", exc_info=True)
            self.notify("Couldn't change Start at login", str(exc) or type(exc).__name__)
        self._read_autostart()


def _dataset_path(dataset_id: str) -> str:
    return f"/v1/datasets/{urllib.parse.quote(dataset_id, safe='')}"


def _tokens(count: object) -> str:
    return "1 token" if count == 1 else f"{count} tokens"


_APPLY_CONFIG = {
    Mode.HOSTED: "Restart, in the menu, applies your changes (and unpairs open schematics).",
    Mode.ATTACHED: "Changes apply when qsv-clientd next starts: restart it.",
}
_APPLY_TO_SERVICE = "Changes apply when the qsv-clientd {} service restarts."
_APPLY_ON_START = "Changes apply when qsv-clientd next starts."
