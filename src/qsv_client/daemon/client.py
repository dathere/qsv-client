"""Run qsv through a running qsv-clientd daemon instead of spawning it yourself.

``DaemonClient.run`` and ``AsyncDaemonClient.run`` take the same arguments as ``Qsv.run``,
return the same ``QsvResult`` and raise the same ``QsvError`` subclasses. Relative paths are
resolved against ``cwd`` (default: this process's working directory), as with a local run.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import os
import socket
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..client import Arg, Qsv, QsvResult
from ..errors import (
    SUCCESS_EXIT_CODES,
    QsvDaemonError,
    QsvDaemonUnavailable,
    error_from_dict,
    error_from_run,
)
from .config import (
    DEFAULT_PORT,
    DaemonConfig,
    default_socket_path,
    local_token_path,
    runtime_dir,
)

# the daemon bounds each run by its timeout; this covers the --capabilities probe and teardown
_EXTRA_WAIT = 120.0


class _Target:
    """Where the daemon listens: a Unix socket (POSIX), or loopback TCP plus the local token."""

    def __init__(
        self, socket_path: str | os.PathLike[str] | None, port: int | None, token: str | None
    ) -> None:
        self.socket_path: str | None = None
        self.port: int | None = None
        self.token = token
        if socket_path is not None or (port is None and sys.platform != "win32"):
            self.socket_path = os.fspath(socket_path or _configured_socket())
            return
        self.port = port or _runtime_port() or DEFAULT_PORT
        if self.token is None:
            try:
                self.token = local_token_path().read_text(encoding="utf-8").strip()
            except OSError:
                raise QsvDaemonUnavailable(
                    f"qsv-clientd is not running (no token at {local_token_path()})"
                ) from None

    def describe(self) -> str:
        return self.socket_path or f"127.0.0.1:{self.port}"

    def headers(self, body: bytes) -> dict[str, str]:
        headers = {
            "Host": f"127.0.0.1:{self.port}" if self.port else "qsv-clientd",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Connection": "close",
        }
        if self.port is not None and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def _configured_socket() -> Path:
    """The socket the daemon was configured with (daemon.json), else the default."""
    try:
        return DaemonConfig.load().socket_path()
    except (OSError, ValueError, TypeError):
        return default_socket_path()


def _runtime_port() -> int | None:
    try:
        info = json.loads((runtime_dir() / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    port = info.get("port") if isinstance(info, dict) else None
    return port if isinstance(port, int) else None


def _run_body(
    command: str,
    args: Sequence[Arg],
    *,
    stdin: bytes | str | None,
    stdout_path: str | os.PathLike[str] | None,
    timeout: float | None,
    env: Mapping[str, str] | None,
    cwd: str,
) -> dict[str, Any]:
    # the daemon runs elsewhere: a relative cwd means relative to this process, as for Qsv
    cwd = os.path.abspath(cwd)
    body: dict[str, Any] = {
        "command": command,
        "args": [os.fspath(a) if isinstance(a, os.PathLike) else str(a) for a in args],
        "cwd": cwd,
        "env": dict(env or {}),
    }
    if stdin is not None:
        raw = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        body["stdin_b64"] = base64.b64encode(raw).decode()
    if stdout_path is not None:
        body["stdout_path"] = str(Path(cwd, os.fspath(stdout_path)))
    if timeout is not None:
        body["timeout"] = timeout
    return body


def _decode(status: int, payload: bytes) -> Any:
    try:
        data = json.loads(payload) if payload else {}
    except ValueError:
        raise QsvDaemonError(f"daemon sent invalid JSON (HTTP {status})", status=status) from None
    if isinstance(data, dict) and isinstance(data.get("daemon_error"), dict):
        err = data["daemon_error"]
        raise QsvDaemonError(
            str(err.get("message", "")), status=status, code=str(err.get("code", "daemon"))
        )
    if status >= 400:
        raise QsvDaemonError(f"daemon answered HTTP {status}", status=status)
    return data


def _finish(data: dict[str, Any], *, command: str, check: bool, text: bool) -> QsvResult:
    if isinstance(data.get("error"), dict):
        raise error_from_dict(data["error"])
    res = data["result"]
    argv = [str(a) for a in res["args"]]
    stderr = str(res["stderr"])
    exit_code = int(res["exit_code"])
    if check and exit_code not in SUCCESS_EXIT_CODES:
        raise error_from_run(exit_code=exit_code, stderr=stderr, args=argv, command=command or None)
    raw = base64.b64decode(res["stdout_b64"]) if res.get("stdout_b64") is not None else None
    stdout_path = Path(res["stdout_path"]) if res.get("stdout_path") else None
    return QsvResult(
        args=tuple(argv),
        exit_code=exit_code,
        stdout=raw.decode("utf-8") if raw is not None and text else None,
        stdout_bytes=raw if raw is not None and not text else None,
        stderr=stderr,
        duration=float(res["duration"]),
        stdout_path=stdout_path,
    )


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        if sys.platform == "win32":
            raise FileNotFoundError("Unix sockets are not used on Windows")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class DaemonClient:
    """Synchronous client for qsv-clientd. See the module docstring."""

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        port: int | None = None,
        token: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> None:
        self._target = _Target(socket_path, port, token)
        self.cwd = os.fspath(cwd) if cwd is not None else None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        wait: float | None = 30.0,
    ) -> Any:
        """Send one API request; returns the decoded JSON reply."""
        payload = json.dumps(body).encode() if body is not None else b""
        t = self._target
        conn: http.client.HTTPConnection
        if t.socket_path is not None:
            conn = _UnixHTTPConnection(t.socket_path, wait)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", t.port, timeout=wait)
        try:
            conn.request(method, path, body=payload, headers=t.headers(payload))
            resp = conn.getresponse()
            status, data = resp.status, resp.read()
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise QsvDaemonUnavailable(f"qsv-clientd is not running at {t.describe()}") from exc
        except OSError as exc:
            raise QsvDaemonError(f"talking to qsv-clientd failed: {exc}") from exc
        finally:
            conn.close()
        return _decode(status, data)

    def available(self) -> bool:
        try:
            return bool(self.request("GET", "/v1/hello", wait=2.0).get("service") == "qsv-clientd")
        except QsvDaemonError:
            return False

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = self.request("GET", "/v1/status")
        return result

    def run(
        self,
        command: str,
        *args: Arg,
        stdin: bytes | str | None = None,
        stdout_path: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        text: bool = True,
    ) -> QsvResult:
        """Run ``qsv <command> <args...>`` in the daemon. Same contract as :meth:`Qsv.run`."""
        body = _run_body(
            command,
            args,
            stdin=stdin,
            stdout_path=stdout_path,
            timeout=timeout,
            env=env,
            cwd=self.cwd or os.getcwd(),
        )
        wait = timeout + _EXTRA_WAIT if timeout is not None else None
        return _finish(
            self.request("POST", "/v1/run", body, wait=wait),
            command=command,
            check=check,
            text=text,
        )


class AsyncDaemonClient:
    """asyncio client for qsv-clientd. Same API as :class:`DaemonClient`, with coroutines."""

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        port: int | None = None,
        token: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> None:
        self._target = _Target(socket_path, port, token)
        self.cwd = os.fspath(cwd) if cwd is not None else None

    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        wait: float | None = 30.0,
    ) -> Any:
        """Send one API request; returns the decoded JSON reply."""
        payload = json.dumps(body).encode() if body is not None else b""
        t = self._target
        try:
            status, data = await asyncio.wait_for(self._exchange(method, path, payload), wait)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise QsvDaemonUnavailable(f"qsv-clientd is not running at {t.describe()}") from exc
        except asyncio.TimeoutError:
            raise QsvDaemonError("qsv-clientd did not answer in time") from None
        except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
            raise QsvDaemonError(f"talking to qsv-clientd failed: {exc}") from exc
        return _decode(status, data)

    async def _exchange(self, method: str, path: str, payload: bytes) -> tuple[int, bytes]:
        t = self._target
        if t.socket_path is not None:
            if sys.platform == "win32":
                raise FileNotFoundError("Unix sockets are not used on Windows")
            reader, writer = await asyncio.open_unix_connection(t.socket_path)
        else:
            reader, writer = await asyncio.open_connection("127.0.0.1", t.port)
        try:
            head = [f"{method} {path} HTTP/1.1"]
            head += [f"{k}: {v}" for k, v in t.headers(payload).items()]
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
            await writer.drain()
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin-1").split("\r\n")
            status = int(lines[0].split(" ", 2)[1])
            length = 0
            for line in lines[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            return status, await reader.readexactly(length)
        finally:
            writer.close()

    async def available(self) -> bool:
        try:
            reply = await self.request("GET", "/v1/hello", wait=2.0)
        except QsvDaemonError:
            return False
        return bool(reply.get("service") == "qsv-clientd")

    async def status(self) -> dict[str, Any]:
        result: dict[str, Any] = await self.request("GET", "/v1/status")
        return result

    async def run(
        self,
        command: str,
        *args: Arg,
        stdin: bytes | str | None = None,
        stdout_path: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        text: bool = True,
    ) -> QsvResult:
        """See :meth:`DaemonClient.run`."""
        body = _run_body(
            command,
            args,
            stdin=stdin,
            stdout_path=stdout_path,
            timeout=timeout,
            env=env,
            cwd=self.cwd or os.getcwd(),
        )
        wait = timeout + _EXTRA_WAIT if timeout is not None else None
        reply = await self.request("POST", "/v1/run", body, wait=wait)
        return _finish(reply, command=command, check=check, text=text)


def qsv_or_daemon(binary: str | None = None, **kwargs: Any) -> DaemonClient | Qsv:
    """A :class:`DaemonClient` when qsv-clientd is running, else a local :class:`Qsv`.

    Only ``cwd`` carries over to the daemon. Any other ``Qsv`` option (a specific
    ``binary``, ``timeout``, ``env``, LLM settings, ``min_version``...) is something the daemon
    can't honor per client, so passing one always returns a local ``Qsv``.
    """
    if binary is not None or set(kwargs) - {"cwd"}:
        return Qsv(binary, **kwargs)
    try:
        client = DaemonClient(cwd=kwargs.get("cwd"))
    except QsvDaemonError:
        return Qsv(binary, **kwargs)
    return client if client.available() else Qsv(binary, **kwargs)
