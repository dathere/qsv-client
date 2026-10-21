"""Shared helpers for the qsv-clientd tests: run a daemon in a thread, talk raw HTTP to it."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from typing import Any

from qsv_client.daemon.host import DaemonHost


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class DaemonThread(DaemonHost):
    """A :class:`DaemonHost` the tests can also talk raw HTTP to."""

    def start(self) -> DaemonThread:
        super().start()
        return self

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
