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
import json
import os
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import ensure_private_dir
from .registry import Dataset, Fingerprint

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


async def fetch_latest(daemon: Daemon, ds: Dataset) -> dict[str, Any]:
    source = ds.source_url or ds.pid
    if not source:
        raise FetchError(409, "no_source", "no source URL or PID is registered for this dataset")
    cache = ensure_private_dir(daemon.home / "cache" / ds.id)
    incoming = cache / ("incoming" + _suffix(source))
    url, via = await _resolve(source)
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    caps = await daemon._qsv.capabilities()
    if caps.has_command("get") and (scheme in _QSV_GET_SCHEMES or scheme in ("http", "https")):
        name = f"qsv-clientd-{ds.id}.csv"
        await daemon._browser_run(
            ds, ["get", url, "--name", name], timeout=daemon.config.max_timeout
        )
        await daemon._browser_run(
            ds,
            ["get", "cache-fetch", name, "--output", str(incoming)],
            timeout=daemon.config.max_timeout,
        )
    elif scheme in _QSV_GET_SCHEMES:
        raise FetchError(501, "needs_qsv_get", f"{scheme}:// sources need qsv's `get` command")
    elif scheme in ("http", "https"):
        await asyncio.to_thread(_download, url, incoming)
    else:
        raise FetchError(422, "bad_source", f"unsupported source scheme {scheme!r}")

    try:
        # a page of HTML or a truncated file must not replace good data
        await daemon._browser_run(ds, ["count", str(incoming)])
        fp = await asyncio.to_thread(Fingerprint.of, incoming)
        changed = fp.sha256 != ds.sha256
        if ds.allow_replace:
            target = Path(ds.original_path or ds.path)
            staged = target.with_name(f".{target.name}.qsv-clientd")
            await asyncio.to_thread(shutil.copyfile, incoming, staged)
            os.replace(staged, target)
            ds.path = str(target)
        else:
            current = cache / ("data" + _suffix(source))
            os.replace(incoming, current)
            if ds.original_path is None:
                ds.original_path = ds.path
            ds.path = str(current)
        daemon.registry.save()
    finally:
        incoming.unlink(missing_ok=True)
    return {"changed": changed, "sha256": fp.sha256, "bytes": fp.size, "via": via}


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


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            if resp.headers.get_content_type() == "text/html":
                raise FetchError(422, "not_data", "the source URL serves a web page, not data")
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp, fh, 1 << 20)
    except FetchError:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise FetchError(502, "download_failed", f"download failed: {exc}") from None
