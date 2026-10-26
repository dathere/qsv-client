"""Fetch the latest copy of a registered dataset from its source URL or PID.

Sources, in order: the ``source_url`` the user registered, else the dataset's PID. A browser
can trigger a fetch but never chooses the URL: both come from the registry.

- ``ckan://``, ``dathere://``, ``s3://``, ``gs://``, ``az://`` (and http(s) when available) go
  through ``qsv get``, which handles conditional requests and decompression (qsv 26+).
- Plain http(s) without ``qsv get``: downloaded with urllib.
- A DOI (``doi:10...``, ``10....``, or a doi.org URL) is resolved with DataCite content
  negotiation, looking for a ``contentUrl``. Most DOIs point at a landing page instead;
  then the user must register ``--source-url``.
- A CKAN resource page (``<site>/dataset/<name>/resource/<id>``) is read as ``ckan://<id>``
  on that site, and a CKAN dataset page (``<site>/dataset/<name>``) as its one tabular
  resource, found with ``package_show``.
- ``ckan://`` sources resolve against the dataset's own CKAN API (``ckan_api``), else the
  daemon's ``QSV_CKAN_API``; with neither they are refused rather than sent to qsv's default
  CKAN site.

Whatever arrives must be data: a web page (a landing page, a sign-in form) is refused before
it can replace anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import glob
import http.client
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._process import _finish_despite_cancel
from ._http import HttpError
from .config import ensure_private_dir
from .registry import Dataset

if TYPE_CHECKING:
    from .server import Daemon

_QSV_GET_SCHEMES = ("ckan", "dathere", "s3", "gs", "az")
_DATA_SUFFIXES = (".csv", ".tsv", ".tab", ".ssv", ".txt")
_TABULAR_FORMATS = ("csv", "tsv", "tab", "ssv", "text/csv", "text/tab-separated-values")
_HTTP_TIMEOUT = 60.0
_METADATA_MAX_BYTES = 8 << 20
_UA = "qsv-clientd"
_CKAN_ACTION_PATH = "/api/3/action"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# CKAN may be mounted below the host's root, so the site is everything before /dataset/
_CKAN_RESOURCE_PAGE = re.compile(
    r"^(?P<site>https?://[^?#]+?)/dataset/[^/?#]+/resource/(?P<rid>" + _UUID + r")/?(?:[?#].*)?$"
)
_CKAN_DATASET_PAGE = re.compile(
    r"^(?P<site>https?://[^?#]+?)/dataset/(?P<name>[^/?#]+)/?(?:[?#].*)?$"
)


class FetchError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


async def fetch_latest(daemon: Daemon, ds: Dataset, *, force: bool = False) -> dict[str, Any]:
    """``force`` skips the column check (the user's call, never a page's)."""
    source = ds.source_url or ds.pid
    if not source:
        raise FetchError(409, "no_source", "no source URL or PID is registered for this dataset")
    # one fetch per dataset at a time: they share the qsv cache entry and the target file
    lock = daemon._fetch_locks.setdefault(ds.id, asyncio.Lock())
    async with lock:
        return await _fetch_locked(daemon, ds, source, force)


async def download(
    daemon: Daemon,
    source: str,
    staging_dir: Path,
    *,
    name: str,
    ckan_api: str | None = None,
    ds: Dataset | None = None,
    what: str = "the source URL",
) -> tuple[Path, str]:
    """Download ``source`` into a new private file in ``staging_dir`` and check that it is data.

    Returns the file, which the caller owns (and must remove or move), and how the source was
    resolved (``url``, ``doi`` or ``ckan``). ``name`` is the qsv cache entry ``get`` uses.
    ``what`` names the source in errors ("the PID", "the source URL").
    """
    # threads can't be cancelled, so a disconnect waits for them (_finish_despite_cancel)
    # before the staging file is removed
    incoming = _private_temp(staging_dir, ".incoming" + _suffix(source))
    try:
        max_bytes, timeout = daemon.config.fetch_max_bytes, daemon.config.fetch_timeout
        target = await _resolve(source, ckan_api, timeout=timeout, downloads=daemon.downloads)
        url = target.url
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        caps = await daemon._qsv.capabilities()
        has_get = caps.has_command("get")
        if scheme == "ckan" and not has_get:
            # the prebuilt qsv has no `get`: look the resource up here, then download its data
            url = await _finish_despite_cancel(
                asyncio.to_thread(
                    _ckan_data_url,
                    _ckan_api_for(target),
                    url,
                    timeout=timeout,
                    downloads=daemon.downloads,
                )
            )
            scheme = urllib.parse.urlsplit(url).scheme.lower()
            if scheme not in ("http", "https"):
                raise FetchError(422, "bad_source", f"the CKAN resource's data is at {url!r}")
        if has_get and (scheme in _QSV_GET_SCHEMES or scheme in ("http", "https")):
            argv = ["get", url, "--name", name]
            if scheme == "ckan":
                argv += ["--ckan-api", _ckan_api_for(target)]
            try:
                await daemon._browser_run(ds, argv, timeout=timeout)
                await daemon._browser_run(
                    ds, ["get", "cache-fetch", name, "--output", str(incoming)], timeout=timeout
                )
            except HttpError as exc:
                if exc.code == "qsv_timeout":
                    raise _timed_out(timeout) from None
                raise
            # qsv puts a new file at --output with the umask's mode (often 0644), not our 0600
            incoming.chmod(0o600)
            # qsv downloads it, so the size can only be checked once it is here
            if incoming.stat().st_size > max_bytes:
                raise _too_large(max_bytes)
        elif scheme in _QSV_GET_SCHEMES:
            raise FetchError(501, "needs_qsv_get", f"{scheme}:// sources need qsv's `get` command")
        elif scheme in ("http", "https"):
            await _finish_despite_cancel(
                asyncio.to_thread(
                    _download,
                    url,
                    incoming,
                    max_bytes=max_bytes,
                    timeout=timeout,
                    downloads=daemon.downloads,
                    what=what,
                )
            )
        else:
            raise FetchError(422, "bad_source", f"unsupported source scheme {scheme!r}")

        # `qsv get` keeps whatever the server sent, a landing page included, and an HTML page
        # can even parse as CSV, so look before `count` turns it into a cryptic CSV error
        if _looks_like_html(incoming):
            raise _web_page(what)
        # every record must parse: a truncated download or ragged rows must not replace good data
        await daemon._browser_run(ds, ["count", str(incoming)])
    except BaseException:
        incoming.unlink(missing_ok=True)
        raise
    return incoming, target.via


async def _fetch_locked(daemon: Daemon, ds: Dataset, source: str, force: bool) -> dict[str, Any]:
    cache = ensure_private_dir(daemon.home / "cache" / ds.id)
    what = "the source URL" if ds.source_url else "the PID"
    incoming, via = await download(
        daemon,
        source,
        cache,
        name=f"qsv-clientd-{ds.id}.csv",
        ckan_api=ds.ckan_api,
        ds=ds,
        what=what,
    )
    try:
        # An error page served as text/plain still "parses" as one-column CSV, so also require
        # every column the data has now (new ones are fine): the viz arguments may name any
        headers = await _headers(daemon, ds, incoming)
        current = Path(ds.path)
        if not force and current.is_file():
            missing = [h for h in await _headers(daemon, ds, current) if h not in headers]
            if missing or not headers:
                raise FetchError(
                    422,
                    "columns_missing",
                    f"the fetched data lacks {len(missing)} of the dataset's columns, so it was "
                    "not used; if the columns were renamed or dropped on purpose, run "
                    f"`qsv-clientd fetch {ds.id} --force`",
                )
        fp = await daemon._fingerprint(incoming, ds.algo, ds)
        changed = fp.digest != ds.digest
        if ds.allow_replace:
            target = Path(ds.original_path or ds.path)
            await _finish_despite_cancel(asyncio.to_thread(_replace_keeping_mode, incoming, target))
            ds.path = str(target)
        else:
            # the file's name is the schematic's title, so keep the registered one's stem
            original = Path(ds.original_path or ds.path)
            current = cache / (original.stem + _suffix(source))
            os.replace(incoming, current)
            previous = Path(ds.path)
            if previous != current and previous.parent == cache:
                previous.unlink(missing_ok=True)
                for sidecar in cache.glob(f"{glob.escape(previous.stem)}.stats.csv*"):
                    if sidecar != current:  # the data itself may be named data.stats.csv
                        sidecar.unlink(missing_ok=True)
            if ds.original_path is None:
                ds.original_path = ds.path
            ds.path = str(current)
        daemon.registry.save()
    finally:
        incoming.unlink(missing_ok=True)
    return {"changed": changed, "fingerprint": fp.public(), "via": via}


def _private_temp(directory: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=".qsv-clientd-", suffix=suffix, dir=directory)
    os.close(fd)
    return Path(name)


async def _headers(daemon: Daemon, ds: Dataset, path: Path) -> list[str]:
    from .server import _parse_csv  # server imports this module

    with tempfile.TemporaryDirectory(prefix="qsv-clientd-") as tmp:
        out = Path(tmp) / "headers.csv"
        await daemon._browser_run(ds, ["slice", "--len", "0", str(path)], stdout_path=out)
        # capped like any browser result, and parsed allowing fields over csv's default limit
        rows = _parse_csv(daemon._capped_text(out), csv.reader)
    return [str(h) for h in rows[0]] if rows else []


def _replace_keeping_mode(src: Path, target: Path) -> None:
    """Atomically replace ``target`` with a copy of ``src``, keeping ``target``'s mode."""
    staged = _private_temp(target.parent, ".tmp")  # 0600 until the mode is set
    try:
        shutil.copyfile(src, staged)
        if target.exists():
            staged.chmod(target.stat().st_mode & 0o777)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _suffix(source: str) -> str:
    path = urllib.parse.urlsplit(source).path.lower()
    return next((s for s in _DATA_SUFFIXES if path.endswith(s)), ".csv")


@dataclass(frozen=True)
class _Target:
    url: str
    via: str
    ckan_api: str | None


async def _resolve(
    source: str, ckan_api: str | None, *, timeout: float, downloads: Downloads
) -> _Target:
    doi = _doi(source)
    if doi is not None:
        url = await asyncio.to_thread(_datacite_content_url, doi)
        if url is None:
            raise FetchError(
                422,
                "pid_not_data",
                f"DOI {doi} does not advertise a downloadable file; "
                "register the data URL with --source-url",
            )
        return _Target(url, "doi", ckan_api)
    page = ckan_resource_page(source)
    if page is not None:
        rid, api = page
        return _Target(f"ckan://{rid}", "ckan", api)
    m = _CKAN_DATASET_PAGE.match(source.strip())
    if m is not None:
        api = m["site"] + _CKAN_ACTION_PATH
        resources = await _finish_despite_cancel(
            asyncio.to_thread(
                _ckan_package_resources,
                api,
                m["name"],
                source,
                timeout=timeout,
                downloads=downloads,
            )
        )
        if resources is not None:  # None: not a CKAN site after all; download it as it is
            return _Target(f"ckan://{_one_tabular(resources, source)}", "ckan", api)
    return _Target(source, "url", ckan_api)


def _ckan_api_for(target: _Target) -> str:
    api = target.ckan_api or os.environ.get("QSV_CKAN_API")
    if not api:
        raise FetchError(
            422,
            "needs_ckan_api",
            f"{target.url} does not say which CKAN site it is on; register the dataset "
            "with --ckan-api <site URL> (or its resource page's URL)",
        )
    return api


def _ckan_data_url(api: str, url: str, *, timeout: float, downloads: Downloads) -> str:
    """The data URL of CKAN resource ``ckan://<id>``, from ``resource_show``, anonymously."""
    rid = url[len("ckan://") :]
    if not rid or rid.endswith("?"):
        raise FetchError(
            501, "needs_qsv_get", "ckan://<name>? sources need qsv's `get` command; use the id"
        )
    show = api.rstrip("/") + "/resource_show?id=" + urllib.parse.quote(rid, safe="")
    try:
        data = _get_json(show, timeout=timeout, downloads=downloads)
    except (OSError, ValueError) as exc:
        raise FetchError(
            502,
            "download_failed",
            f"CKAN at {api} did not show resource {rid} ({exc}); a private resource needs "
            "qsv's `get` command and QSV_CKAN_TOKEN",
        ) from None
    result = data.get("result") if isinstance(data, dict) else None
    data_url = result.get("url") if isinstance(result, dict) else None
    if not isinstance(data_url, str) or not data_url:
        raise FetchError(422, "not_data", f"CKAN resource {rid} has no data URL")
    return data_url


def ckan_resource_page(url: str) -> tuple[str, str] | None:
    """``(resource id, Action API URL)`` for a CKAN resource page URL, else None.

    A resource's ``/download/<file>`` URL is not a page: it serves the data itself.
    """
    m = _CKAN_RESOURCE_PAGE.match(url.strip())
    if m is None:
        return None
    return m["rid"].lower(), m["site"] + _CKAN_ACTION_PATH


def ckan_api_url(value: str) -> str:
    """The CKAN Action API URL for ``value``: a site URL gets ``/api/3/action`` appended."""
    url = value.strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"the CKAN API must be an http(s) URL, not {value!r}")
    if "/api/" in parts.path + "/":
        return url
    return url + _CKAN_ACTION_PATH


def _ckan_package_resources(
    api: str, name: str, page: str, *, timeout: float, downloads: Downloads
) -> list[dict[str, Any]] | None:
    """The resources of CKAN dataset ``name``, or None when ``api`` doesn't answer as CKAN."""
    show = api + "/package_show?id=" + urllib.parse.quote(name, safe="")
    try:
        data = _get_json(show, timeout=timeout, downloads=downloads)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # CKAN's answer for a private dataset, which is no reason to fetch the page itself.
            # The daemon sends no token here: it would go to a host named only by the URL
            raise FetchError(
                403,
                "private_dataset",
                f"CKAN would not list the resources of {page} (a private dataset?); register "
                "the resource's own page URL instead: private data needs qsv's `get` command "
                "and QSV_CKAN_TOKEN in qsv-clientd's environment",
            ) from None
        return None
    except (OSError, ValueError):
        return None
    result = data.get("result") if isinstance(data, dict) and data.get("success") else None
    resources = result.get("resources") if isinstance(result, dict) else None
    if not isinstance(resources, list):
        return None
    return [r for r in resources if isinstance(r, dict) and isinstance(r.get("id"), str)]


def _one_tabular(resources: list[dict[str, Any]], page: str) -> str:
    def tabular(r: dict[str, Any]) -> bool:
        fmt = str(r.get("format") or "").strip().lower()
        path = urllib.parse.urlsplit(str(r.get("url") or "")).path.lower()
        return fmt in _TABULAR_FORMATS or path.endswith(_DATA_SUFFIXES)

    found = [r for r in resources if tabular(r)]
    if len(found) == 1:
        return str(found[0]["id"])
    if not found:
        raise FetchError(
            422, "not_data", f"the CKAN dataset {page} has no CSV or TSV resource to fetch"
        )
    listed = "; ".join(f"{r.get('name') or r['id']}: {r.get('url') or r['id']}" for r in found)
    raise FetchError(
        422,
        "ambiguous_source",
        f"the CKAN dataset {page} has {len(found)} tabular resources; register the one you "
        f"want with --source-url ({listed})",
    )


def _looks_like_html(path: Path) -> bool:
    with path.open("rb") as fh:
        head = fh.read(1024)
    head = head.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _web_page(what: str) -> FetchError:
    # "the URL": one given to `qsv-clientd viz`, which has no --source-url to fix it with
    fix = (
        "use the data file's download URL instead"
        if what == "the URL"
        else "register the data file's download URL with --source-url"
    )
    return FetchError(422, "not_data", f"{what} serves a web page, not data; {fix}")


def _doi(source: str) -> str | None:
    s = source.strip()
    if s.lower().startswith("doi:"):
        return s[4:]
    if s.startswith("10.") and "/" in s:
        return s
    parts = urllib.parse.urlsplit(s)
    if parts.hostname in ("doi.org", "dx.doi.org") and parts.path.startswith("/10."):
        return urllib.parse.unquote(parts.path[1:])
    return None


def _datacite_content_url(doi: str) -> str | None:
    req = urllib.request.Request(
        "https://doi.org/" + urllib.parse.quote(doi, safe="/"),
        headers={"Accept": "application/vnd.datacite.datacite+json", "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            if "json" not in resp.headers.get("Content-Type", ""):
                return None
            data = json.load(resp)
    except (OSError, ValueError):
        return None
    urls = data.get("contentUrl") if isinstance(data, dict) else None
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list):
        return None
    return next(
        (u for u in urls if isinstance(u, str) and u.lower().startswith(("https://", "http://"))),
        None,
    )


def _download(
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    timeout: float,
    downloads: Downloads,
    what: str = "the source URL",
) -> None:
    deadline = time.monotonic() + timeout
    watchdog = _Watchdog(timeout)
    downloads.start(watchdog)
    opener = urllib.request.build_opener(
        _WatchedHTTP(watchdog), _WatchedHTTPS(watchdog), _DefaultPortRedirects()
    )
    req = urllib.request.Request(_drop_default_port(url), headers={"User-Agent": _UA})
    try:
        with opener.open(req, timeout=min(_HTTP_TIMEOUT, timeout)) as resp:
            if resp.headers.get_content_type() == "text/html":
                raise _web_page(what)
            length = resp.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise _too_large(max_bytes)
            # read1 returns whatever has arrived, so the size is checked as data comes
            read = getattr(resp, "read1", resp.read)
            written = 0
            with dest.open("wb") as fh:
                while chunk := read(1 << 20):
                    written += len(chunk)
                    if written > max_bytes:
                        raise _too_large(max_bytes)
                    fh.write(chunk)
        if watchdog.fired:  # the shut socket can read as a clean end of the data
            raise _cut_off(watchdog, timeout)
    except FetchError:
        dest.unlink(missing_ok=True)
        raise
    except (OSError, http.client.HTTPException) as exc:
        dest.unlink(missing_ok=True)
        # a socket timeout of the whole fetch_timeout ends at the deadline too: the clock may
        # not show it yet (Windows ticks every 15.6ms), and the watchdog may not have fired
        idle_to_deadline = isinstance(exc, TimeoutError) and timeout <= _HTTP_TIMEOUT
        if watchdog.fired or idle_to_deadline or time.monotonic() > deadline:
            raise _cut_off(watchdog, timeout) from None
        raise FetchError(502, "download_failed", f"download failed: {exc}") from None
    finally:
        watchdog.cancel()
        downloads.finish(watchdog)


def _get_json(url: str, *, timeout: float, downloads: Downloads) -> Any:
    """``url``'s JSON, read under a download watchdog: bounded in time and size, and cut off
    when the daemon closes. Fails with OSError (urllib's HTTPError for a 4xx/5xx answer) or
    ValueError, or FetchError at the deadline or the size limit."""
    watchdog = _Watchdog(timeout)
    downloads.start(watchdog)
    opener = urllib.request.build_opener(_WatchedHTTP(watchdog), _WatchedHTTPS(watchdog))
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _UA})
    try:
        with opener.open(req, timeout=min(_HTTP_TIMEOUT, timeout)) as resp:
            body = resp.read(_METADATA_MAX_BYTES + 1)
        if watchdog.fired:
            raise _cut_off(watchdog, timeout)
    except (OSError, http.client.HTTPException) as exc:
        # as in _download: a socket timeout of the whole budget is the deadline too
        idle_to_deadline = isinstance(exc, TimeoutError) and timeout <= _HTTP_TIMEOUT
        if watchdog.fired or idle_to_deadline:
            raise _cut_off(watchdog, timeout) from None
        if isinstance(exc, OSError):
            raise
        raise OSError(str(exc)) from None
    finally:
        watchdog.cancel()
        downloads.finish(watchdog)
    if len(body) > _METADATA_MAX_BYTES:
        raise FetchError(
            502, "download_failed", f"{url} answered with over {_METADATA_MAX_BYTES >> 20} MiB"
        )
    return json.loads(body)


def _cut_off(watchdog: _Watchdog, timeout: float) -> FetchError:
    return _shutting_down() if watchdog.aborted else _timed_out(timeout)


def _shutting_down() -> FetchError:
    return FetchError(503, "shutting_down", "qsv-clientd is shutting down; the download stopped")


class Downloads:
    """The downloads in progress, so a closing daemon can cut them off. A download runs on a
    thread its request must wait for (threads can't be cancelled), so without this, closing
    the daemon (the tray's Quit, or SIGTERM) would wait for it, up to fetch_timeout."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[_Watchdog] = set()
        self._closed = False

    def start(self, watchdog: _Watchdog) -> None:
        """Register a download about to connect; refused once the daemon is closing."""
        with self._lock:
            if self._closed:
                watchdog.cancel()
                raise _shutting_down()
            self._active.add(watchdog)

    def finish(self, watchdog: _Watchdog) -> None:
        with self._lock:
            self._active.discard(watchdog)

    def abort_all(self) -> None:
        with self._lock:
            self._closed = True
            for watchdog in self._active:
                watchdog.abort()


class _Watchdog:
    """Shuts the download's sockets at the deadline, so no read (headers included) outlasts it.

    Socket timeouts bound each read, not the download: a server sending a byte at a time
    would otherwise keep the connection, the worker thread and the dataset's lock busy.
    """

    def __init__(self, seconds: float) -> None:
        self.fired = False
        self.aborted = False
        """Fired because the daemon is closing, not at the deadline."""
        # the sockets themselves: urllib drops conn.sock once the headers are in, while the
        # response goes on reading the body from the same socket
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, http_class: Any) -> Any:
        def create(*args: Any, **kwargs: Any) -> http.client.HTTPConnection:
            conn: http.client.HTTPConnection = http_class(*args, **kwargs)
            open_socket = conn.connect

            def connect() -> None:
                open_socket()
                with self._lock:
                    self._socks.append(conn.sock)
                    if self.fired:  # connected after the deadline, when there was nothing to shut
                        self._shut(conn.sock)

            setattr(conn, "connect", connect)  # noqa: B010 (mypy: can't assign to a method)
            return conn

        return create

    @staticmethod
    def _shut(sock: socket.socket) -> None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            for sock in self._socks:
                self._shut(sock)

    def abort(self) -> None:
        self.aborted = True
        self._fire()

    def cancel(self) -> None:
        self._timer.cancel()


def _drop_default_port(url: str) -> str:
    """``url`` without an explicit default port (``https://h:443/`` → ``https://h/``).

    urllib sends the URL's host and port as ``Host``. A presigned S3 URL (CKAN's S3 storage
    redirects to one, written with ``:443``) is signed over a ``Host`` without the default
    port, so sending it with one fails with 403 SignatureDoesNotMatch.
    """
    parts = urllib.parse.urlsplit(url)
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    try:
        port = parts.port
    except ValueError:
        return url
    if default is None or port != default or parts.hostname is None:
        return url
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = f"{userinfo}@{host}" if userinfo else host
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


class _DefaultPortRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        return super().redirect_request(req, fp, code, msg, headers, _drop_default_port(newurl))


class _WatchedHTTP(urllib.request.HTTPHandler):
    def __init__(self, watchdog: _Watchdog) -> None:
        super().__init__()
        self._watchdog = watchdog

    def do_open(self, http_class: Any, req: Any, **kwargs: Any) -> Any:
        return super().do_open(self._watchdog.watch(http_class), req, **kwargs)


class _WatchedHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, watchdog: _Watchdog) -> None:
        super().__init__()
        self._watchdog = watchdog

    def do_open(self, http_class: Any, req: Any, **kwargs: Any) -> Any:
        # https_open passes its own context arguments, which differ across Python versions
        return super().do_open(self._watchdog.watch(http_class), req, **kwargs)


def _timed_out(timeout: float) -> FetchError:
    return FetchError(
        504, "download_timeout", f"the download took longer than {timeout:g}s (fetch_timeout)"
    )


def _too_large(max_bytes: int) -> FetchError:
    return FetchError(413, "too_large", f"the download is over {max_bytes} bytes (fetch_max_bytes)")
