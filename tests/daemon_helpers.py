"""Shared helpers for the qsv-clientd tests: run a daemon in a thread, talk raw HTTP to it."""

from __future__ import annotations

import asyncio
import http.client
import json
import threading
from dataclasses import dataclass
from typing import Any

from qsv_client.daemon.server import Daemon


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class DaemonThread:
    """A :class:`Daemon` serving from its own event loop, so sync clients can call it."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._main, daemon=True)

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.daemon.start())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self.loop.run_forever()

    def start(self) -> DaemonThread:
        self._thread.start()
        assert self._ready.wait(60), "daemon did not start"
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        if self._error is None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.daemon.close(), self.loop).result(30)
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(30)

    @property
    def port(self) -> int:
        assert self.daemon.port is not None
        return self.daemon.port

    def http(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        origin: str | None = "null",
        token: str | None = None,
        host: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        """A request as a browser would send it (loopback TCP, ``Origin: null`` by default)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if origin is not None:
            h["Origin"] = origin
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            return Reply(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
        finally:
            conn.close()

    def pair(self, digest: str) -> str:
        """Pair like a schematic would, approving as the local user; returns the token."""
        req = self.http("POST", "/v1/pair", {"digest": digest}).json()
        approved = self.http(
            "POST",
            f"/v1/pairings/{req['code']}/approve",
            origin=None,
            token=self.daemon.local_token,
        )
        assert approved.status == 200, approved.body
        poll = self.http("GET", f"/v1/pair/{req['request_id']}").json()
        assert poll["status"] == "approved"
        token: str = poll["token"]
        return token
