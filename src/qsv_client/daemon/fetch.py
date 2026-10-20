"""Fetch the latest copy of a registered dataset from its source URL or PID.

Sources, in order: the ``source_url`` the user registered, else the dataset's PID. A browser
can trigger a fetch but never chooses the URL: both come from the registry.

- ``ckan://``, ``dathere://``, ``s3://``, ``gs://``, ``az://`` (and http(s) when available) go
  through ``qsv get``, which handles conditional requests and decompression (qsv 26+).
- Plain http(s) without ``qsv get``: downloaded with urllib.
- A DOI (``doi:10...``, ``10....``, or a doi.org URL) is resolved with DataCite content
  negotiation, looking for a ``contentUrl``. Most DOIs point at a landing page instead;
  then the user must register ``--source-url``.
"""

from __future__ import annotations

import asyncio
import csv
import glob
import json
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._process import _finish_despite_cancel
from .config import ensure_private_dir
from .registry import Dataset

if TYPE_CHECKING:
    from .server import Daemon

_QSV_GET_SCHEMES = ("ckan", "dathere", "s3", "gs", "az")
_DATA_SUFFIXES = (".csv", ".tsv", ".tab", ".ssv", ".txt")
_HTTP_TIMEOUT = 60.0
_UA = "qsv-clientd"


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


async def _fetch_locked(daemon: Daemon, ds: Dataset, source: str, force: bool) -> dict[str, Any]:
    cache = ensure_private_dir(daemon.home / "cache" / ds.id)
    # threads can't be cancelled, so a disconnect waits for them (_finish_despite_cancel)
    # before the staging file is removed and the lock released
    incoming = _private_temp(cache, ".incoming" + _suffix(source))
    try:
        url, via = await _resolve(source)
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        caps = await daemon._qsv.capabilities()
        max_bytes, timeout = daemon.config.fetch_max_bytes, daemon.config.fetch_timeout
        if caps.has_command("get") and (scheme in _QSV_GET_SCHEMES or scheme in ("http", "https")):
            name = f"qsv-clientd-{ds.id}.csv"
            await daemon._browser_run(ds, ["get", url, "--name", name], timeout=timeout)
            await daemon._browser_run(
                ds, ["get", "cache-fetch", name, "--output", str(incoming)], timeout=timeout
            )
            # qsv downloads it, so the size can only be checked once it is here
            if incoming.stat().st_size > max_bytes:
                raise _too_large(max_bytes)
        elif scheme in _QSV_GET_SCHEMES:
            raise FetchError(501, "needs_qsv_get", f"{scheme}:// sources need qsv's `get` command")
        elif scheme in ("http", "https"):
            await _finish_despite_cancel(
                asyncio.to_thread(_download, url, incoming, max_bytes=max_bytes, timeout=timeout)
            )
        else:
            raise FetchError(422, "bad_source", f"unsupported source scheme {scheme!r}")

        # every record must parse: a truncated download or ragged rows must not replace good data
        await daemon._browser_run(ds, ["count", str(incoming)])
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


async def _resolve(source: str) -> tuple[str, str]:
    doi = _doi(source)
    if doi is None:
        return source, "url"
    url = await asyncio.to_thread(_datacite_content_url, doi)
    if url is None:
        raise FetchError(
            422,
            "pid_not_data",
            f"DOI {doi} does not advertise a downloadable file; "
            "register the data URL with --source-url",
        )
    return url, "doi"


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


def _download(url: str, dest: Path, *, max_bytes: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=min(_HTTP_TIMEOUT, timeout)) as resp:
            if resp.headers.get_content_type() == "text/html":
                raise FetchError(422, "not_data", "the source URL serves a web page, not data")
            length = resp.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise _too_large(max_bytes)
            # read1 returns whatever has arrived, so a server that trickles bytes can't hold a
            # read open past the deadline (each read still waits up to the socket timeout)
            read = getattr(resp, "read1", resp.read)
            written = 0
            with dest.open("wb") as fh:
                while chunk := read(1 << 20):
                    written += len(chunk)
                    if written > max_bytes:
                        raise _too_large(max_bytes)
                    if time.monotonic() > deadline:
                        raise FetchError(
                            504,
                            "download_timeout",
                            f"the download took longer than {timeout:g}s (fetch_timeout)",
                        )
                    fh.write(chunk)
    except FetchError:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise FetchError(502, "download_failed", f"download failed: {exc}") from None


def _too_large(max_bytes: int) -> FetchError:
    return FetchError(413, "too_large", f"the download is over {max_bytes} bytes (fetch_max_bytes)")
