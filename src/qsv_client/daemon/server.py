"""The qsv-clientd daemon: one ``AsyncQsv`` binary shared by local processes and paired pages.

Two trust tiers:

- **local**: a connection on the Unix socket (owner-only permissions), or a loopback TCP
  request carrying the per-user local token (from a file only this user can read) and no
  ``Origin`` header. Equivalent to the user running qsv: any command, stdin, ``stdout_path``.
- **browser**: any other loopback request. It may detect the daemon (``/v1/hello``), ask to
  pair, and, with a paired token, run allowlisted read-only operations on that one dataset.
  It never names a file and never sees one: paths are stripped from what it gets back.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import io
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, ClassVar, Literal

from .. import __version__
from ..client import AsyncQsv, QsvResult, find_qsv
from ..errors import EXIT_TIMEOUT, SUCCESS_EXIT_CODES, QsvError, QsvTimeout, error_from_run
from . import fetch
from ._http import HttpError, Request, Response, read_body, read_head, write_response
from .allowlist import (
    CSV_OUTPUT,
    NO_SIDE_FILES,
    NON_CSV_FLAGS,
    QUERY_ENV,
    NotAllowed,
    build_query,
    exact_match_regex,
)
from .config import (
    API_VERSION,
    DaemonConfig,
    ensure_private_dir,
    local_token_path,
    runtime_dir,
    socket_path_error,
    state_dir,
    write_private_file,
)
from .pairing import Pairing, token_digest
from .registry import Dataset, Fingerprint, Registry, SchematicSource, validate_viz_args
from .results import LOCAL_KEY, ResultStore, key_for_grant

log = logging.getLogger("qsv_client.daemon")

Tier = Literal["local", "browser"]
_LOCAL_BODY_LIMIT = 64 * 1024 * 1024
_BROWSER_BODY_LIMIT = 64 * 1024
_MAX_ROWS = 500
_PAGE_ROWS = 100  # a stored result's first page, and /page's default length
_HEAD_TIMEOUT = 10.0
"""Seconds a client gets to send its request head; an idle connection can't hold us forever."""
_BODY_TIMEOUT = 60.0
"""Seconds a client gets to send its request body once the head is in."""
_FLUSH_TIMEOUT = 30.0
"""Seconds to flush a response to a client before dropping the connection."""
_MAX_TCP_CONNECTIONS = 64
"""Open loopback connections allowed at once. Other local users can reach the loopback port
(not the 0600 socket); without a cap they could use up our file descriptors."""


class Daemon:
    def __init__(
        self,
        config: DaemonConfig | None = None,
        *,
        home: Path | None = None,
        tcp_port: int | None = None,
        socket_path: Path | None = None,
    ) -> None:
        self.config = config or DaemonConfig()
        self.binary = find_qsv(self.config.binary)
        self.home = home or state_dir()
        self.registry = Registry(self.home / "datasets.json")
        self.pairing = Pairing(self.config.token_ttl)
        self.local_token = secrets.token_urlsafe(32)
        self.jobs = asyncio.Semaphore(self.config.max_jobs)
        self.running = 0
        # None: no loopback listener (config port 0); 0: an ephemeral port (tests)
        self._tcp_port = (self.config.port or None) if tcp_port is None else tcp_port
        self._socket_path = socket_path or self.config.socket_path()
        self.port: int | None = None
        """The bound loopback port, once started."""
        self._servers: list[asyncio.Server] = []
        self._owns_socket = False
        self._connections: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._tcp_open = 0
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        self.results = ResultStore(self)
        self._started = False
        self._qsv = self._client()

    # -- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        caps = await self._qsv.capabilities()
        log.info("qsv-clientd %s using %s %s", __version__, caps.binary, caps.version)
        ensure_private_dir(self.home)
        # bind everything before publishing the token: a second instance that fails to bind
        # must not replace the running daemon's credentials
        try:
            await self._bind()
        except BaseException:
            await self._close_servers()
            raise
        # Tokens live in memory only, so any result left on disk belongs to no page now. Only
        # once bound: a second instance must not touch the running daemon's results.
        self.results.clear()
        self._started = True
        token_file = (
            local_token_path()
            if self._socket_path == self.config.socket_path()
            else (self._socket_path.parent / "local-token")
        )
        write_private_file(token_file, self.local_token.encode())
        self._token_file = token_file
        info = {"pid": os.getpid(), "port": self.port, "socket": str(self._socket_path)}
        write_private_file(token_file.parent / "daemon.json", json.dumps(info).encode())

    async def _bind(self) -> None:
        if sys.platform != "win32":
            parent = self._socket_path.parent
            if parent == runtime_dir():
                ensure_private_dir(parent)
            else:
                # a directory the user chose (maybe shared, like /tmp): don't change its mode
                parent.mkdir(parents=True, exist_ok=True)
            await self._remove_stale_socket()
            # owner-only from the moment it exists, not just after a chmod
            old_umask = os.umask(0o177)
            try:
                server = await asyncio.start_unix_server(
                    lambda r, w: self._on_connection(r, w, "unix"),
                    path=self._socket_path,
                    limit=64 * 1024,
                )
            except OSError as exc:
                message = socket_path_error(self._socket_path, exc)
                if message is None:
                    raise
                raise ValueError(message) from None
            finally:
                os.umask(old_umask)
            self._owns_socket = True
            self._servers.append(server)
            self._socket_path.chmod(0o600)
        # Windows has no Unix socket here, so local clients need loopback too
        if self._tcp_port is not None or sys.platform == "win32":
            server = await asyncio.start_server(
                lambda r, w: self._on_connection(r, w, "tcp"),
                host="127.0.0.1",
                port=self._tcp_port or 0,
                limit=64 * 1024,
            )
            self.port = server.sockets[0].getsockname()[1]
            self._servers.append(server)

    async def _remove_stale_socket(self) -> None:
        if sys.platform == "win32" or not self._socket_path.exists():
            return
        try:
            _, writer = await asyncio.open_unix_connection(self._socket_path)
        except OSError:
            self._socket_path.unlink(missing_ok=True)
            return
        writer.close()
        raise RuntimeError(f"another qsv-clientd is already listening on {self._socket_path}")

    async def close(self) -> None:
        await self._close_servers()
        if self._started:
            self.results.clear()
        with contextlib.suppress(AttributeError):
            self._token_file.unlink(missing_ok=True)
            (self._token_file.parent / "daemon.json").unlink(missing_ok=True)

    async def _close_servers(self) -> None:
        for server in self._servers:
            server.close()
        # since Python 3.12 wait_closed() waits for open connections; end ours first (a
        # cancelled run kills its qsv tree like any other cancellation)
        for writer in list(self._writers):
            writer.transport.abort()  # drop unsent output: shutdown must not wait on a peer
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
        # only our own socket: never one a running instance is still serving on
        if self._owns_socket:
            self._socket_path.unlink(missing_ok=True)
            self._owns_socket = False

    async def serve_forever(self) -> None:
        await self.start()
        stop = asyncio.Event()
        if sys.platform != "win32":
            # launchd and systemd stop services with SIGTERM; without a handler Python dies
            # at once and leaves the socket, local-token and daemon.json behind
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            await self.close()

    def _client(self, cwd: str | None = None) -> AsyncQsv:
        return AsyncQsv(
            self.binary,
            cwd=cwd,
            inherit_env=self.config.inherit_env,
            kill_grace=self.config.kill_grace,
        )

    # -- connection handling ---------------------------------------------------------------

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, transport: str
    ) -> None:
        if transport == "tcp":
            if self._tcp_open >= _MAX_TCP_CONNECTIONS:
                writer.transport.abort()
                return
            self._tcp_open += 1
        try:
            await self._serve_connection(reader, writer, transport)
        finally:
            if transport == "tcp":
                self._tcp_open -= 1

    async def _serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, transport: str
    ) -> None:
        self._writers.add(writer)
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
            task.add_done_callback(self._connections.discard)
        request: Request | None = None
        response: Response | None
        try:
            try:
                try:
                    request = await asyncio.wait_for(read_head(reader), _HEAD_TIMEOUT)
                except asyncio.TimeoutError:
                    raise HttpError(408, "timeout", "request head not received in time") from None
                tier = self._tier(request, transport)
                limit = _LOCAL_BODY_LIMIT if tier == "local" else _BROWSER_BODY_LIMIT
                try:
                    await asyncio.wait_for(read_body(reader, request, limit), _BODY_TIMEOUT)
                except asyncio.TimeoutError:
                    raise HttpError(408, "timeout", "request body not received in time") from None
                if request.method == "OPTIONS":
                    response = Response(204)
                else:
                    response = await self._until_disconnect(reader, self._route(request, tier))
            except HttpError as exc:
                response = Response.error(exc)
            except (ConnectionError, asyncio.IncompleteReadError):
                return
            except Exception:
                log.exception("internal error")
                response = Response.error(HttpError(500, "internal", "internal daemon error"))
            if response is None:  # client went away; its run was cancelled
                return
            origin = request.headers.get("origin") if request is not None else None
            # only pages allowed to pair may read replies, so other sites can't detect us
            if (
                transport == "tcp"
                and request is not None
                and origin is not None
                and origin in self.config.browser_origins
            ):
                self._cors(response, origin, request)
            with contextlib.suppress(ConnectionError):
                await write_response(writer, response)
        finally:
            writer.close()
            # close() flushes first: a peer that stopped reading would hold us here forever.
            # Stay in _writers until done, so a shutdown that starts meanwhile can abort us
            try:
                await asyncio.wait_for(writer.wait_closed(), _FLUSH_TIMEOUT)
            except (asyncio.TimeoutError, ConnectionError):
                writer.transport.abort()
            except BaseException:  # cancelled by shutdown: don't leave the transport flushing
                writer.transport.abort()
                raise
            finally:
                self._writers.discard(writer)

    async def _until_disconnect(
        self, reader: asyncio.StreamReader, work: Awaitable[Response]
    ) -> Response | None:
        """Run ``work``; cancel it (killing any qsv it started) if the client disconnects."""
        task = asyncio.ensure_future(work)

        async def eof() -> None:
            while await reader.read(4096):
                pass

        watcher = asyncio.ensure_future(eof())
        try:
            done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return None
        finally:
            watcher.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _tier(self, request: Request, transport: str) -> Tier:
        if transport == "unix":
            return "local"
        host = request.headers.get("host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            # DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends its own Host
            raise HttpError(421, "bad_host", "requests must be addressed to 127.0.0.1")
        auth = request.headers.get("authorization", "")
        if "origin" not in request.headers and secrets.compare_digest(
            auth.encode(), f"Bearer {self.local_token}".encode()
        ):
            return "local"
        return "browser"

    def _cors(self, response: Response, origin: str, request: Request) -> None:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
        # Private Network Access, and its successor Local Network Access (Chrome)
        if "access-control-request-private-network" in request.headers:
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        if "access-control-request-local-network" in request.headers:
            response.headers["Access-Control-Allow-Local-Network"] = "true"

    # -- routing ---------------------------------------------------------------------------

    async def _route(self, request: Request, tier: Tier) -> Response:
        parts = [p for p in request.path.split("/") if p]
        if not parts or parts[0] != f"v{API_VERSION}":
            raise HttpError(404, "not_found", "unknown route")
        route = parts[1:]
        method = request.method
        match route:
            case ["hello"] if method == "GET":
                return Response.json({"service": "qsv-clientd", "api": API_VERSION})
            case ["pair"] if method == "POST":
                return self._pair(request, tier)
            case ["pair", request_id] if method == "GET":
                return self._pair_poll(request_id)
            case ["datasets", dataset_id] if method == "GET":
                ds = self._dataset_for(request, tier, dataset_id)
                return Response.json(ds.public() if tier == "browser" else _private(ds))
            case ["datasets", dataset_id, "disconnect"] if method == "POST":
                # a page withdrawing its own token; its stored result goes with it
                self._dataset_for(request, tier, dataset_id)
                withdrawn = tier == "browser" and self.pairing.revoke(_bearer(request))
                self.results.sweep(self.pairing.live_digests())
                return Response.json({"revoked": int(withdrawn)})
            case ["datasets", dataset_id, action] if method == "POST":
                ds = self._dataset_for(request, tier, dataset_id)
                handler = self._dataset_actions.get(action)
                if handler is None:
                    raise HttpError(404, "not_found", f"unknown dataset action {action!r}")
                self.results.sweep(self.pairing.live_digests())
                return await handler(self, ds, request.json(), tier, _result_key(request, tier))
        if tier != "local":
            raise HttpError(401, "unauthorized", "this route is only for local clients")
        match route:
            case ["status"] if method == "GET":
                return Response.json(await self._status())
            case ["run"] if method == "POST":
                return await self._run(request.json())
            case ["datasets"] if method == "GET":
                return Response.json([_private(d) for d in self.registry.datasets.values()])
            case ["datasets"] if method == "POST":
                return await self._register(request.json())
            case ["datasets", dataset_id] if method == "DELETE":
                removed = self.registry.remove(dataset_id)
                revoked = self.pairing.revoke_all(dataset_id)
                self.results.remove_dataset(dataset_id)
                if not removed:
                    raise HttpError(404, "not_found", "no such dataset")
                return Response.json({"removed": dataset_id, "revoked": revoked})
            case ["pairings"] if method == "GET":
                return Response.json(self._pending())
            case ["pairings", code, "approve" | "deny" as verb] if method == "POST":
                return self._decide(code, verb)
            case ["tokens", "revoke"] if method == "POST":
                target = request.json().get("dataset_id")
                revoked = self.pairing.revoke_all(target if isinstance(target, str) else None)
                self.results.sweep(self.pairing.live_digests())
                return Response.json({"revoked": revoked})
        raise HttpError(404, "not_found", "unknown route")

    # -- local routes ----------------------------------------------------------------------

    async def _status(self) -> dict[str, Any]:
        caps = await self._qsv.capabilities()
        return {
            "service": "qsv-clientd",
            "api": API_VERSION,
            "daemon_version": __version__,
            "pid": os.getpid(),
            "binary": self.binary,
            "qsv": {"binary": caps.binary, "version": caps.version, "commands": len(caps.commands)},
            "jobs": {"max": self.config.max_jobs, "running": self.running},
            "datasets": len(self.registry.datasets),
            "pending_pairings": len(self.pairing.pending),
            "port": self.port,
            "socket": str(self._socket_path) if sys.platform != "win32" else None,
        }

    async def _run(self, body: dict[str, Any]) -> Response:
        command = body.get("command")
        if not isinstance(command, str):
            raise HttpError(422, "bad_request", "command must be a string")
        args = _str_list(body.get("args", []), "args")
        cwd = body.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
            raise HttpError(422, "bad_request", "cwd must be an absolute path")
        env = body.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise HttpError(422, "bad_request", "env must map strings to strings")
        stdin: bytes | None = None
        if isinstance(body.get("stdin_b64"), str):
            try:
                stdin = base64.b64decode(body["stdin_b64"], validate=True)
            except ValueError:
                raise HttpError(422, "bad_request", "stdin_b64 is not base64") from None
        stdout_path = body.get("stdout_path")
        if stdout_path is not None and not isinstance(stdout_path, str):
            raise HttpError(422, "bad_request", "stdout_path must be a string")
        timeout = self._timeout(body.get("timeout"), self.config.max_timeout)
        qsv = self._client(cwd)
        try:
            async with self._job(wait=timeout):
                result = await qsv.run(
                    command,
                    *args,
                    stdin=stdin,
                    stdout_path=stdout_path,
                    timeout=timeout,
                    env=env,
                    check=False,
                    text=False,
                )
        except QsvError as exc:
            return Response.json({"error": exc.to_dict()})
        except OSError as exc:  # e.g. an unwritable stdout_path or a missing cwd, as locally
            return Response.json({"os_error": _os_error_dict(exc)})
        return Response.json({"result": _result_dict(result)})

    async def _register(self, body: dict[str, Any]) -> Response:
        path = _abs_path(body.get("path"), "path")
        if not path.is_file():
            raise HttpError(422, "not_found", f"no such file: {path}")
        viz_args = _str_list(body.get("viz_args", []), "viz_args")
        try:
            validate_viz_args(viz_args)
        except ValueError as exc:
            raise HttpError(422, "not_allowed", str(exc)) from None
        schematic = body.get("schematic_path")
        schematic_path = _abs_path(schematic, "schematic_path") if schematic else None
        fingerprint = await self._fingerprint(path)
        source = None
        if schematic_path is not None and schematic_path.is_file():
            html = await asyncio.to_thread(schematic_path.read_text, "utf-8", "replace")
            source = SchematicSource.from_html(html)
            page = source.fingerprint if source is not None else None
            # a requested render replaces the stale schematic anyway
            if page is not None and not body.get("force") and not body.get("render"):
                current = fingerprint
                if page.algo != current.algo:
                    current = await self._fingerprint(path, page.algo)
                if page.digest != current.digest:
                    raise HttpError(
                        409,
                        "fingerprint_mismatch",
                        "the schematic was rendered from different data than this file "
                        "(re-render it, or pass force)",
                    )
        ds = self.registry.add(
            path,
            fingerprint=fingerprint,
            viz_args=viz_args,
            schematic_path=schematic_path,
            source=source,
            title=_opt(body.get("title")),
            pid=_opt(body.get("pid")),
            source_url=_opt(body.get("source_url")),
            allow_replace=body.get("allow_replace") is True,
        )
        if body.get("render"):
            await self._render_to_disk(ds)
        return Response.json(_private(ds))

    def _pending(self) -> list[dict[str, Any]]:
        out = []
        for p in self.pairing.pending.values():
            ds = self.registry.datasets.get(p.dataset_id) if p.dataset_id else None
            out.append(
                {
                    "code": p.code,
                    "origin": p.origin,
                    "dataset": _private(ds) if ds else None,
                    "approved": p.token is not None,
                    "denied": p.denied,
                }
            )
        return out

    def _decide(self, code: str, verb: str) -> Response:
        pending = self.pairing.find_code(code)
        if pending is None:
            raise HttpError(404, "not_found", "no pending pairing with that code (expired?)")
        if verb == "deny":
            self.pairing.deny(code)
            return Response.json({"denied": pending.code})
        if not pending.dataset_id or pending.dataset_id not in self.registry.datasets:
            raise HttpError(
                409,
                "unknown_schematic",
                "this schematic matches no registered dataset; register it first "
                "(qsv-clientd register <data> --schematic <html>) and pair again",
            )
        self.pairing.approve(code)
        return Response.json({"approved": pending.code, "dataset_id": pending.dataset_id})

    # -- pairing (any tier) ----------------------------------------------------------------

    def _pair(self, request: Request, tier: Tier) -> Response:
        if tier == "browser":
            if not self.config.browser:
                raise HttpError(403, "disabled", "browser access is turned off")
            origin = request.headers.get("origin", "")
            if origin not in self.config.browser_origins:
                raise HttpError(403, "bad_origin", f"origin {origin!r} may not pair")
        else:
            origin = "local"
        body = request.json()
        ds = self.registry.match(_opt(body.get("schematic_uuid")), _opt(body.get("digest")))
        # the same answer whether or not the schematic is registered, so a page can't probe
        # which datasets this user has; `qsv-clientd approve` explains a non-match
        pending = self.pairing.request(ds.id if ds else "", origin)
        if pending is None:
            raise HttpError(429, "rate_limited", "too many pairing requests; try again shortly")
        return Response.json(
            {
                "request_id": pending.request_id,
                "code": pending.code,
                "approve_with": (f"qsv-clientd approve {pending.code}"),
            }
        )

    def _pair_poll(self, request_id: str) -> Response:
        status, token = self.pairing.poll(request_id)
        if status == "approved" and token is not None:
            grant = self.pairing.check(token)
            ds = self.registry.datasets.get(grant.dataset_id) if grant else None
            return Response.json(
                {
                    "status": status,
                    "token": token,
                    "expires_in": self.config.token_ttl,
                    "dataset": ds.public() if ds else None,
                }
            )
        return Response.json({"status": status})

    def _dataset_for(self, request: Request, tier: Tier, dataset_id: str) -> Dataset:
        if tier == "browser":
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
            grant = self.pairing.check(token) if token else None
            if grant is None or grant.dataset_id != dataset_id:
                raise HttpError(401, "unauthorized", "pair this schematic first")
        ds = self.registry.datasets.get(dataset_id)
        if ds is None:
            raise HttpError(404, "not_found", "no such dataset")
        return ds

    # -- dataset actions (local, or browser with a token for that dataset) -----------------

    async def _rows(self, ds: Dataset, body: dict[str, Any], tier: Tier, key: str) -> Response:
        offset = _int(body.get("offset", 0), "offset", 0, 2**62)
        limit = _int(body.get("limit", 100), "limit", 1, _MAX_ROWS)
        column, value = body.get("column"), body.get("value")
        if value is not None:
            if not isinstance(column, str) or not column or '"' in column:
                raise HttpError(422, "bad_request", "column must be a header name")
            if not isinstance(value, str):
                raise HttpError(422, "bad_request", "value must be a string")
            regex = exact_match_regex(value)

            async def matches(out: Path) -> None:
                await self._browser_run(
                    ds,
                    ["search", "--select", f'"{column}"', regex, ds.path],
                    stdout_path=out,
                    no_match_ok=True,
                )

            # kept, so the page can read the matches a page at a time without searching again
            stored = await self.results.create(key, ds, matches)
            page = await self.results.page(
                key, ds, stored.id, start=offset, length=limit, search="", order=None
            )
            return Response.json(
                {
                    "headers": stored.headers,
                    "rows": page["rows"],
                    "offset": offset,
                    "result": stored.public(),
                }
            )
        with tempfile.TemporaryDirectory(prefix="qsv-clientd-") as tmp:
            out = Path(tmp) / "slice.csv"
            await self._browser_run(
                ds,
                ["slice", "--start", str(offset), "--len", str(limit), ds.path],
                stdout_path=out,
            )
            rows = _parse_csv(self._capped_text(out), csv.reader)
        headers, data = (rows[0], rows[1:]) if rows else ([], [])
        return Response.json({"headers": headers, "rows": data, "offset": offset})

    async def _refresh(self, ds: Dataset, body: dict[str, Any], tier: Tier, key: str) -> Response:
        fp = await self._fingerprint(Path(ds.path), ds.algo, ds)
        count = await self._browser_run(ds, ["count", ds.path])
        out: dict[str, Any] = {
            "changed": fp.digest != ds.digest,
            "fingerprint": fp.public(),
            "rows": int(count.strip() or 0),
        }
        if body.get("stats"):
            with tempfile.TemporaryDirectory(prefix="qsv-clientd-") as tmp:
                stats_file = Path(tmp) / "stats.csv"
                await self._browser_run(
                    ds, ["stats", *NO_SIDE_FILES["stats"], ds.path], stdout_path=stats_file
                )
                out["stats"] = _parse_csv(self._capped_text(stats_file), csv.DictReader)
        return Response.json(out)

    async def _query(self, ds: Dataset, body: dict[str, Any], tier: Tier, key: str) -> Response:
        command = body.get("command")
        args = body.get("args", [])
        if not isinstance(command, str) or not isinstance(args, list):
            raise HttpError(422, "bad_request", "command must be a string and args a list")
        caps = await self._qsv.capabilities()
        try:
            argv = build_query(command, args, frozenset(caps.commands))
        except NotAllowed as exc:
            raise HttpError(403, "not_allowed", str(exc)) from None
        is_csv = command in CSV_OUTPUT and not NON_CSV_FLAGS.intersection(args)
        with tempfile.TemporaryDirectory(prefix="qsv-clientd-") as tmp:
            out = Path(tmp) / "out"
            await self._browser_run(ds, [*argv, ds.path], stdout_path=out, env=QUERY_ENV)
            size = out.stat().st_size
            with out.open("rb") as fh:
                data = fh.read(self.config.browser_max_output)
            truncated = size > len(data)
            text = data.decode("utf-8", "replace")
            reply: dict[str, Any] = {"stdout": text, "truncated": truncated, "bytes": size}
            if is_csv and truncated:
                # too big to send whole: keep it, and let the page read it a page at a time
                stored = await self.results.adopt(key, ds, out)
                first = await self.results.page(
                    key, ds, stored.id, start=0, length=_PAGE_ROWS, search="", order=None
                )
                reply.update(headers=stored.headers, rows=first["rows"], result=stored.public())
            elif is_csv:
                reply.update(_table(text))
        return Response.json(reply)

    async def _page(self, ds: Dataset, body: dict[str, Any], tier: Tier, key: str) -> Response:
        start = _int(body.get("start", 0), "start", 0, 2**62)
        length = _int(body.get("length", _PAGE_ROWS), "length", 1, _MAX_ROWS)
        search = body.get("search", "")
        if not isinstance(search, str):
            raise HttpError(422, "bad_request", "search must be a string")
        search = search.strip()
        order = body.get("order")
        parsed: tuple[int, bool] | None = None
        if order is not None:
            if not isinstance(order, dict) or not isinstance(order.get("desc", False), bool):
                raise HttpError(422, "bad_request", "order must be {column, desc}")
            parsed = (_int(order.get("column"), "order.column", 0, 2**31), order.get("desc", False))
        return Response.json(
            await self.results.page(
                key,
                ds,
                body.get("result"),
                start=start,
                length=length,
                search=search,
                order=parsed,
            )
        )

    async def _render(self, ds: Dataset, body: dict[str, Any], tier: Tier, key: str) -> Response:
        html = await self._render_to_disk(ds)
        if tier == "local":
            return Response.json({"schematic_path": ds.schematic_path, "bytes": len(html)})
        if len(html) > self.config.browser_max_render:
            raise HttpError(413, "too_large", "the re-rendered schematic is too large to return")
        return Response(200, html, "text/html; charset=utf-8")

    async def _fetch(self, ds: Dataset, body: dict[str, Any], tier: Tier, key: str) -> Response:
        if tier == "browser" and not self.config.allow_fetch:
            raise HttpError(403, "disabled", "fetching is turned off")
        # only the user may accept data whose columns changed; a page can't ask for that
        force = tier == "local" and body.get("force") is True
        try:
            result = await fetch.fetch_latest(self, ds, force=force)
        except fetch.FetchError as exc:
            raise HttpError(exc.status, exc.code, _scrub(str(exc), ds)) from None
        return Response.json(result)

    _dataset_actions: ClassVar[
        dict[str, Callable[[Daemon, Dataset, dict[str, Any], Tier, str], Awaitable[Response]]]
    ] = {
        "rows": _rows,
        "page": _page,
        "refresh": _refresh,
        "query": _query,
        "render": _render,
        "fetch": _fetch,
    }

    # -- helpers ---------------------------------------------------------------------------

    async def _render_to_disk(self, ds: Dataset) -> bytes:
        """Re-run ``qsv viz smart`` with the registered arguments; returns the new HTML."""
        if ds.schematic_path is None and not ds.viz_args:
            raise HttpError(409, "cannot_render", "no schematic or viz arguments registered")
        target_dir = Path(ds.schematic_path).parent if ds.schematic_path else Path(self.home)
        ensure_private_dir(self.home)
        fd, tmp_name = tempfile.mkstemp(prefix=".qsv-clientd-", suffix=".html", dir=target_dir)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            # fingerprint first: the schematic describes the data as it was when rendering began
            fp = await self._fingerprint(Path(ds.path), ds=ds)
            await self._browser_run(
                ds,
                ["viz", "smart", *ds.viz_args, ds.path, "--output", str(tmp)],
                timeout=self.config.max_timeout,
                # the new page's bridge must probe this daemon, whatever port it is on
                env={"QSV_CLIENTD_PORT": str(self.port)} if self.port else None,
            )
            html = tmp.read_bytes()
            source = SchematicSource.from_html(html.decode("utf-8", "replace"))
            ds.schematic_uuid = source.schematic_uuid if source else None
            ds.fingerprint = fp
            if ds.schematic_path:
                target = Path(ds.schematic_path)
                # mkstemp made it 0600; keep the schematic's existing mode (or a normal 0644)
                mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
                tmp.chmod(mode)
                os.replace(tmp, target)
            self.registry.save()
            return html
        finally:
            tmp.unlink(missing_ok=True)

    def _capped_text(self, path: Path) -> str:
        """A browser result file's text, refusing it before reading if it is over the cap."""
        size = path.stat().st_size
        if size > self.config.browser_max_output:
            raise HttpError(
                413,
                "too_large",
                f"result is {size} bytes, over the {self.config.browser_max_output}-byte limit; "
                "ask for fewer rows",
            )
        # newline="": keep a CR inside a quoted field (read_text would turn CRLF into LF)
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read()

    async def _fingerprint(
        self, path: Path, algo: str | None = None, ds: Dataset | None = None
    ) -> Fingerprint:
        """BLAKE3 of ``path`` by default, else ``algo``.

        The standard library has no BLAKE3, so qsv's ``blake3`` command computes it
        (memory-mapped, on all cores). Every qsv that renders schematics has the command; a qsv
        without it (qsvlite) gets SHA-256 instead.
        """
        has_blake3 = (await self._qsv.capabilities()).has_command("blake3")
        if algo is None:
            algo = "blake3" if has_blake3 else "sha256"
        elif algo == "blake3" and not has_blake3:
            raise HttpError(501, "no_blake3", "this qsv has no `blake3` command (use qsv or qsvdp)")
        if algo == "sha256":
            return await asyncio.to_thread(Fingerprint.sha256_of, path)
        size = path.stat().st_size
        out = await self._browser_run(
            ds, ["blake3", "--no-names", str(path)], timeout=self.config.max_timeout
        )
        return Fingerprint("blake3", out.strip().lower(), size)

    async def _browser_run(
        self,
        ds: Dataset | None,
        argv: list[str],
        *,
        stdout_path: Path | None = None,
        no_match_ok: bool = False,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run a daemon-built argv for a dataset action; errors come back with paths scrubbed."""
        try:
            async with self._job():
                result = await self._qsv.run(
                    argv[0],
                    *argv[1:],
                    stdout_path=stdout_path,
                    timeout=timeout or self.config.browser_timeout,
                    env=env,
                    check=False,
                )
        except QsvError as exc:
            raise _qsv_http_error(exc, ds) from None
        if result.exit_code not in SUCCESS_EXIT_CODES:
            err = error_from_run(
                exit_code=result.exit_code, stderr=result.stderr, args=(), command=argv[0]
            )
            if no_match_ok and err.kind == "no_match":
                if stdout_path is not None and not stdout_path.exists():
                    stdout_path.write_text("")
                return ""
            raise _qsv_http_error(err, ds)
        return result.stdout or ""

    @contextlib.asynccontextmanager
    async def _job(self, wait: float | None = None) -> Any:
        """Hold one of the ``max_jobs`` slots. With ``wait``, give up after that many seconds
        with :class:`QsvTimeout`: a caller's timeout covers the time queued, not just qsv."""
        if wait is None:
            await self.jobs.acquire()
        else:
            try:
                await asyncio.wait_for(self.jobs.acquire(), wait)
            except asyncio.TimeoutError:
                raise QsvTimeout(
                    f"timed out after {wait}s waiting for one of the daemon's "
                    f"{self.config.max_jobs} job slots; qsv did not run",
                    exit_code=EXIT_TIMEOUT,
                ) from None
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self.jobs.release()

    def _timeout(self, value: object, cap: float) -> float:
        if value is None:
            return cap
        if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
            raise HttpError(422, "bad_request", "timeout must be a positive number")
        return min(float(value), cap)


def _result_dict(result: QsvResult) -> dict[str, Any]:
    return {
        "args": list(result.args),
        "exit_code": result.exit_code,
        "stdout_b64": (
            None if result.stdout_bytes is None else base64.b64encode(result.stdout_bytes).decode()
        ),
        "stderr": result.stderr,
        "duration": result.duration,
        "stdout_path": str(result.stdout_path) if result.stdout_path else None,
    }


def _result_key(request: Request, tier: Tier) -> str:
    """Whose stored result a request reads: its paired page's, or the local user's."""
    if tier != "browser":
        return LOCAL_KEY
    return key_for_grant(token_digest(_bearer(request)))


def _bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ")


def _private(ds: Dataset) -> dict[str, Any]:
    return asdict(ds)


def _scrub(text: str, ds: Dataset | None) -> str:
    if ds is not None:
        label = ds.title or "dataset"
        for path in filter(None, (ds.path, ds.original_path, ds.schematic_path)):
            text = text.replace(path, label)
    tmp = tempfile.gettempdir()
    return text.replace(tmp, "<tmp>") if tmp else text


def _qsv_http_error(exc: QsvError, ds: Dataset | None) -> HttpError:
    err = HttpError(502, "qsv_error", _scrub(exc.message, ds))
    err.code = f"qsv_{exc.kind}"
    return err


def _os_error_dict(exc: OSError) -> dict[str, Any]:
    return {
        "errno": exc.errno,
        # an OSError made from just a message has no strerror; keep the message
        "strerror": exc.strerror if exc.strerror is not None else str(exc),
        "filename": exc.filename if isinstance(exc.filename, str) else None,
    }


def _table(text: str) -> dict[str, Any]:
    """``headers`` and ``rows`` for a page to show as a table, or nothing if ``text`` isn't CSV.

    Only for whole output: output over the cap is stored and read a page at a time instead.
    """
    try:
        records = _parse_csv(text, csv.reader)
    except HttpError:
        return {}
    if not records:
        return {}
    return {"headers": records[0], "rows": records[1:]}


def _parse_csv(text: str, reader: Any) -> list[Any]:
    # the text is already under browser_max_output, so no field can be longer than that
    csv.field_size_limit(max(csv.field_size_limit(), len(text) + 1))
    try:
        return list(reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise HttpError(502, "bad_output", f"could not parse qsv's CSV output: {exc}") from None


def _str_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(v, str | int | float) and not isinstance(v, bool) for v in value
    ):
        raise HttpError(422, "bad_request", f"{name} must be a list of strings")
    return [str(v) for v in value]


def _abs_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise HttpError(422, "bad_request", f"{name} must be an absolute path")
    return Path(value)


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise HttpError(422, "bad_request", f"{name} must be an integer in [{lo}, {hi}]")
    return value


def remove_runtime_files() -> None:
    """Best-effort cleanup used by ``qsv-clientd uninstall``."""
    shutil.rmtree(runtime_dir(), ignore_errors=True)
