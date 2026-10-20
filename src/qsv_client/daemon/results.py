"""Stored results that a paired page reads one page at a time.

A query whose CSV output is over ``browser_max_output``, and every filtered drill-down, is kept
on disk so the page can ask for any page of it, searched and ordered by qsv, instead of
receiving a truncated copy. Each caller (a paired page's token, or ``"local"``) has at most
one, under ``<home>/results/<key>/``; a new one replaces it. Everything here is written by
qsv into that daemon-owned directory, never beside the user's data.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import secrets
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._http import HttpError
from .allowlist import contains_regex
from .config import ensure_private_dir

if TYPE_CHECKING:
    from .registry import Dataset
    from .server import Daemon

LOCAL_KEY = "local"


def key_for_grant(digest: str) -> str:
    """The results directory name for a paired page: not the token's own digest."""
    return hashlib.sha256(f"qsv-clientd results {digest}".encode()).hexdigest()[:32]


Order = tuple[int, bool]  # (0-based column, descending)


@dataclass
class _View:
    search: str
    order: Order | None
    path: Path
    filtered: int


@dataclass
class Stored:
    id: str
    key: str
    dataset_id: str
    data_path: str  # the dataset file it was computed from
    data_version: tuple[int, int] | None  # its size and mtime then
    directory: Path
    headers: list[str]
    total: int
    view: _View | None = None

    @property
    def result(self) -> Path:
        return self.directory / "result.csv"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "total": self.total, "headers": self.headers}


class ResultStore:
    def __init__(self, daemon: Daemon) -> None:
        self._daemon = daemon
        self.root = daemon.home / "results"
        self._results: dict[str, Stored] = {}
        # one per caller: creating, deriving and paging its result never overlap
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- lifetime

    def clear(self) -> None:
        """Remove every stored result (at startup tokens are gone, so they are orphans)."""
        self._results.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    def sweep(self, live_digests: set[str]) -> None:
        """Remove results whose token expired or was revoked."""
        live = {key_for_grant(d) for d in live_digests} | {LOCAL_KEY}
        for key in [k for k in self._results if k not in live]:
            self._drop(key)
        if self.root.is_dir():
            for path in self.root.iterdir():
                if path.name not in live:
                    shutil.rmtree(path, ignore_errors=True)

    def remove_dataset(self, dataset_id: str) -> None:
        for key in [k for k, r in self._results.items() if r.dataset_id == dataset_id]:
            self._drop(key)

    def discard(self, key: str) -> None:
        """Forget ``key``'s result: its next query stored nothing to replace it with."""
        self._drop(key)

    def _drop(self, key: str) -> None:
        stored = self._results.pop(key, None)
        if stored is not None:
            shutil.rmtree(stored.directory, ignore_errors=True)

    # ---- creating

    async def create(
        self, key: str, ds: Dataset, produce: Callable[[Path], Awaitable[None]]
    ) -> Stored:
        """Replace ``key``'s result with the CSV ``produce`` writes to the path it is given."""
        async with self._locks.setdefault(key, asyncio.Lock()):
            self._drop(key)
            directory = ensure_private_dir(self.root / key)
            stored = Stored(
                id=secrets.token_hex(8),
                key=key,
                dataset_id=ds.id,
                data_path=ds.path,
                # before qsv reads it, so a change while it runs makes the result stale too
                data_version=_version(ds.path),
                directory=directory,
                headers=[],
                total=0,
            )
            try:
                await produce(stored.result)
                self._check_size(stored.result)
                await self._index(ds, stored.result)
                stored.headers = await self._headers(ds, stored.result)
                stored.total = await self._count(ds, stored.result)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            if not self._publishable(key, ds):
                # the page disconnected, or the dataset was forgotten, while qsv ran
                shutil.rmtree(directory, ignore_errors=True)
                raise HttpError(410, "result_gone", "the page disconnected while this ran")
            self._results[key] = stored
            return stored

    def _publishable(self, key: str, ds: Dataset) -> bool:
        if self._daemon.registry.datasets.get(ds.id) is None:
            return False
        live = {key_for_grant(d) for d in self._daemon.pairing.live_digests()}
        return key == LOCAL_KEY or key in live

    async def adopt(self, key: str, ds: Dataset, path: Path) -> Stored:
        """Store a CSV file qsv already wrote (it is moved, not copied when on one disk)."""

        async def move(target: Path) -> None:
            await asyncio.to_thread(shutil.move, path, target)

        return await self.create(key, ds, move)

    # ---- reading

    async def page(
        self,
        key: str,
        ds: Dataset,
        result_id: object,
        *,
        start: int,
        length: int,
        search: str,
        order: Order | None,
    ) -> dict[str, Any]:
        async with self._locks.setdefault(key, asyncio.Lock()):
            stored = self._results.get(key)
            if stored is None or result_id != stored.id or stored.dataset_id != ds.id:
                raise HttpError(410, "result_gone", "that result was replaced; run the query again")
            if (stored.data_path, stored.data_version) != (ds.path, _version(ds.path)):
                raise HttpError(409, "result_stale", "the data changed; run the query again")
            if order is not None and not 0 <= order[0] < len(stored.headers):
                raise HttpError(422, "bad_request", "order column is out of range")
            view_path, filtered = await self._view(ds, stored, search, order)
            rows: list[list[str]] = []
            if filtered and start < filtered:
                rows = await self._slice(ds, view_path, start, length)
        return {
            "headers": stored.headers,
            "rows": rows,
            "start": start,
            "total": stored.total,
            "filtered": filtered,
        }

    async def _view(
        self, ds: Dataset, stored: Stored, search: str, order: Order | None
    ) -> tuple[Path, int]:
        if not search and order is None:
            return stored.result, stored.total
        view = stored.view
        if view is not None and (view.search, view.order) == (search, order):
            return view.path, view.filtered
        stored.view = None
        source = stored.result
        filtered = stored.total
        if search:
            searched = stored.directory / "search.csv"
            await self._run(
                ds,
                ["search", "--ignore-case", contains_regex(search), str(source)],
                searched,
                no_match_ok=True,
            )
            source = searched
            filtered = await self._count(ds, searched) if searched.stat().st_size else 0
        if order is not None and filtered:
            column, desc = order
            ordered = stored.directory / "view.csv"
            argv = ["sort", "--natural", "--select", str(column + 1), str(source)]
            if desc:
                argv.insert(1, "--reverse")
            await self._run(ds, argv, ordered)
            source = ordered
        if filtered:
            await self._index(ds, source)
        stored.view = _View(search, order, source, filtered)
        return source, filtered

    # ---- qsv

    async def _run(
        self, ds: Dataset, argv: list[str], out: Path, *, no_match_ok: bool = False
    ) -> None:
        await self._daemon._browser_run(ds, argv, stdout_path=out, no_match_ok=no_match_ok)
        self._check_size(out)

    def _check_size(self, path: Path) -> None:
        cap = self._daemon.config.browser_max_result
        size = path.stat().st_size
        if size > cap:
            path.unlink(missing_ok=True)
            raise HttpError(
                413,
                "too_large",
                f"the result is {size} bytes, over the {cap}-byte limit (browser_max_result)",
            )

    async def _index(self, ds: Dataset, path: Path) -> None:
        caps = await self._daemon._qsv.capabilities()
        if caps.has_command("index"):
            # without an index, slice still works, reading from the start
            with contextlib.suppress(HttpError):
                await self._daemon._browser_run(ds, ["index", str(path)])

    async def _count(self, ds: Dataset, path: Path) -> int:
        out = await self._daemon._browser_run(ds, ["count", str(path)])
        try:
            return int(out.strip() or 0)
        except ValueError:
            raise HttpError(502, "bad_output", "qsv count printed no number") from None

    async def _headers(self, ds: Dataset, path: Path) -> list[str]:
        if not path.stat().st_size:
            return []
        rows = await self._slice(ds, path, 0, 0, with_headers=True)
        return rows[0] if rows else []

    async def _slice(
        self, ds: Dataset, path: Path, start: int, length: int, *, with_headers: bool = False
    ) -> list[list[str]]:
        from .server import _parse_csv  # server imports this module

        page = path.with_name(f".page-{secrets.token_hex(4)}.csv")
        try:
            await self._daemon._browser_run(
                ds,
                ["slice", "--start", str(start), "--len", str(length), str(path)],
                stdout_path=page,
            )
            records = _parse_csv(self._daemon._capped_text(page), csv.reader)
        finally:
            page.unlink(missing_ok=True)
        return records if with_headers else records[1:]


def _version(path: str) -> tuple[int, int] | None:
    """What changes when a file is rewritten in place (a fetch with --allow-replace, an edit)."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)
