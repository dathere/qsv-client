"""The :class:`Qsv` (blocking) and :class:`AsyncQsv` (asyncio) clients."""

from __future__ import annotations

import asyncio
import copy
import csv
import errno
import io
import json
import os
import shutil
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

from . import _process
from .capabilities import Capabilities, from_capabilities_json, parse_version
from .errors import (
    EXIT_TIMEOUT,
    SUCCESS_EXIT_CODES,
    QsvError,
    QsvNotFound,
    QsvTimeout,
    QsvVersionError,
    error_from_run,
)

#: binaries tried, in order, when none is given and ``QSV_BIN`` is unset
DEFAULT_BINARIES = ("qsv", "qsvmcp", "qsvdp", "qsvlite")

#: oldest qsv this client supports: the first with ``--capabilities`` and ``QSV_ERROR_FORMAT``
MIN_QSV_VERSION = "25.0.0"

Arg = str | os.PathLike[str] | int | float


def _with_pathext(cand: str) -> list[Path]:
    """``cand``, then on Windows ``cand`` + each ``PATHEXT`` extension (``C:/qsv/qsv`` ->
    ``C:/qsv/qsv.exe``). ``shutil.which`` only does this for a path from Python 3.12."""
    paths = [Path(cand)]
    if _process.IS_WINDOWS and not Path(cand).suffix:
        exts = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        paths += [Path(cand + ext) for ext in exts if ext]
    return paths


def find_qsv(binary: str | os.PathLike[str] | None = None) -> str:
    """Resolve the qsv binary: ``binary`` -> ``$QSV_BIN`` -> first of qsv/qsvmcp/qsvdp/qsvlite
    on ``PATH``."""
    candidates: list[str] = []
    if binary is not None:
        candidates.append(os.fspath(binary))
    elif os.environ.get("QSV_BIN"):
        candidates.append(os.environ["QSV_BIN"])
    else:
        candidates.extend(DEFAULT_BINARIES)
    for cand in candidates:
        if os.sep in cand or (os.altsep and os.altsep in cand):
            # absolute, so a relative path still resolves when the run sets cwd=. Not
            # normalized: collapsing `..` lexically would be wrong after a symlinked dir.
            for path in _with_pathext(cand):
                if path.is_file():
                    return str(path.absolute())
            continue
        found = shutil.which(cand)
        if found:
            return str(Path(found).absolute())
    raise QsvNotFound(
        f"no qsv binary found (tried {', '.join(candidates)}). Install qsv "
        "(https://github.com/dathere/qsv/releases), put it on PATH, or pass binary= / set QSV_BIN."
    )


@dataclass(frozen=True)
class QsvResult:
    """A finished qsv run.

    ``stdout`` is decoded UTF-8 text, or ``None`` when it was written to ``stdout_path``
    (or when ``text=False``, in which case use ``stdout_bytes``).
    """

    args: tuple[str, ...]
    exit_code: int
    stdout: str | None
    stdout_bytes: bytes | None
    stderr: str
    duration: float
    stdout_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code in SUCCESS_EXIT_CODES

    def json(self) -> Any:
        """Parse stdout (or the ``stdout_path`` file) as JSON."""
        return json.loads(self._text())

    def csv_rows(self) -> list[dict[str, str]]:
        """Parse stdout (or the ``stdout_path`` file) as CSV with a header row."""
        return list(csv.DictReader(io.StringIO(self._text(), newline="")))

    def _text(self) -> str:
        if self.stdout is not None:
            return self.stdout
        if self.stdout_path is not None:
            return self.stdout_path.read_text(encoding="utf-8")
        if self.stdout_bytes is not None:
            return self.stdout_bytes.decode("utf-8")
        raise ValueError("this run captured no stdout")


class _Base:
    def __init__(
        self,
        binary: str | os.PathLike[str] | None = None,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        inherit_env: bool = True,
        cwd: str | os.PathLike[str] | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        min_version: str | None = None,
        kill_grace: float = 5.0,
    ) -> None:
        """
        Args:
            binary: path or name of the qsv binary. Default: ``$QSV_BIN``, else the first of
                qsv, qsvmcp, qsvdp, qsvlite found on ``PATH``.
            timeout: default per-run timeout in seconds (None = no limit). On timeout the run's
                whole process group is sent SIGTERM, then SIGKILL after ``kill_grace`` seconds,
                and :class:`QsvTimeout` is raised.
            env: extra environment variables for every run.
            inherit_env: start from ``os.environ`` (True) or from an empty environment
                (on Windows, one holding only ``SYSTEMROOT``).
            cwd: working directory for every run.
            llm_api_key / llm_base_url / llm_model: passed to ``describegpt`` as
                ``QSV_LLM_APIKEY`` / ``QSV_LLM_BASE_URL`` / ``QSV_LLM_MODEL`` environment
                variables, so the key never appears in the process table.
            min_version: raise :class:`QsvVersionError` on first use if the binary is older.
                The binary must be at least :data:`MIN_QSV_VERSION` (25.0.0) regardless;
                this can only raise that floor. A string that is not a version
                (``"major.minor.patch"``) raises ``ValueError`` here.
            kill_grace: seconds to wait between the polite stop (SIGTERM, or
                ``CTRL_BREAK_EVENT`` on Windows) and the forced kill on timeout or
                cancellation, and the most spent collecting output after the kill.
        """
        if min_version is not None:
            parse_version(min_version)  # fail now, not on every run
        self.binary = find_qsv(binary)
        self.timeout = timeout
        self.inherit_env = inherit_env
        self.cwd = os.fspath(cwd) if cwd is not None else None
        self.kill_grace = kill_grace
        self.min_version = min_version
        self._env: dict[str, str] = dict(env or {})
        for var, value in (
            ("QSV_LLM_APIKEY", llm_api_key),
            ("QSV_LLM_BASE_URL", llm_base_url),
            ("QSV_LLM_MODEL", llm_model),
        ):
            if value is not None:
                self._env[var] = value
        self._capabilities: Capabilities | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(binary={self.binary!r})"

    def _build_env(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        env = dict(os.environ) if self.inherit_env else {}
        if not self.inherit_env and _process.IS_WINDOWS and "SYSTEMROOT" in os.environ:
            # Windows processes (the C runtime, Winsock) can fail to start without it
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        env.update(self._env)
        if extra:
            env.update(extra)
        # the whole point: failures come back as one parseable line
        env["QSV_ERROR_FORMAT"] = "json"
        return env

    def _argv(self, command: str, args: Sequence[Arg]) -> list[str]:
        argv = [self.binary]
        if command:
            argv.append(command)
        argv.extend(os.fspath(a) if isinstance(a, os.PathLike) else str(a) for a in args)
        return argv

    def _check_min_version(self, caps: Capabilities) -> None:
        required = MIN_QSV_VERSION
        if self.min_version and parse_version(self.min_version) > parse_version(required):
            required = self.min_version
        if caps.version_tuple < parse_version(required):
            raise QsvVersionError(
                f"{self.binary} is qsv {caps.version}; at least {required} is required",
                exit_code=0,
                args=[self.binary, "--capabilities"],
            )

    def _finish(
        self,
        raw: _process.RawResult,
        *,
        argv: list[str],
        command: str,
        started: float,
        timeout: float | None,
        text: bool,
        stdout_path: Path | None,
        check: bool,
    ) -> QsvResult:
        duration = time.monotonic() - started
        stderr = raw.stderr.decode("utf-8", errors="replace")
        if raw.timed_out:
            raise QsvTimeout(
                f"timed out after {timeout}s and was killed",
                exit_code=EXIT_TIMEOUT,
                command=command or None,
                args=argv,
                stderr=stderr,
            )
        # before decoding stdout, so undecodable output can't mask the typed error
        if check and raw.exit_code not in SUCCESS_EXIT_CODES:
            raise error_from_run(
                exit_code=raw.exit_code,
                stderr=stderr,
                args=argv,
                command=command or None,
            )
        captured = stdout_path is None
        return QsvResult(
            args=tuple(argv),
            exit_code=raw.exit_code,
            stdout=raw.stdout.decode("utf-8") if captured and text else None,
            stdout_bytes=raw.stdout if captured and not text else None,
            stderr=stderr,
            duration=duration,
            stdout_path=stdout_path,
        )


def _open_stdout(
    stdout_path: str | os.PathLike[str] | None, cwd: str | None
) -> tuple[Path | None, IO[bytes] | None]:
    if stdout_path is None:
        return None, None
    path = Path(stdout_path)
    if cwd is not None and not path.is_absolute():
        # like qsv's own --output: relative to the run's working directory
        path = Path(cwd) / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, path.open("wb")


def _caps_from_probe(binary: str, res: QsvResult) -> Capabilities:
    """Capabilities from a ``--capabilities`` run."""
    if res.exit_code != 0:
        # qsv before 25.0.0 rejects the flag as a usage error ("Unknown flag: '--capabilities'");
        # any other failure (a crash, a signal, a startup error) keeps its own diagnostics
        if "Unknown flag: '--capabilities'" in res.stderr:
            raise QsvVersionError(
                f"{binary} does not support --capabilities; "
                f"qsv {MIN_QSV_VERSION} or later is required",
                exit_code=res.exit_code,
                args=res.args,
                stderr=res.stderr,
            )
        raise error_from_run(
            exit_code=res.exit_code, stderr=res.stderr, args=res.args, command=None
        )
    try:
        caps = from_capabilities_json(res.stdout or "")
        parse_version(caps.version)
    except (ValueError, TypeError, AttributeError) as exc:
        raise QsvError(
            f"could not determine the qsv version from --capabilities: {exc}",
            exit_code=res.exit_code,
            args=res.args,
            stderr=res.stderr,
        ) from exc
    return caps


# Successful --capabilities probes are cached per binary for the whole process and shared by
# every Qsv and AsyncQsv. The key includes size and mtime so a binary upgraded in place is
# probed again. Each client still applies its own min_version. Concurrent probes are deduped
# per key among threads, and among coroutines on one event loop.
_CapsKey = tuple[str, int, int]
_caps_cache: dict[_CapsKey, Capabilities] = {}
_sync_probe_locks: dict[_CapsKey, threading.Lock] = {}
# asyncio.Lock is bound to the loop it is first contended on and then references it, so the
# locks are held weakly: a strong value would keep its weakly-keyed loop alive forever
_async_probe_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.WeakValueDictionary[_CapsKey, asyncio.Lock]
] = weakref.WeakKeyDictionary()
_probe_locks_guard = threading.Lock()

#: upper bound on the --capabilities probe; a shorter run timeout bounds it further
_PROBE_TIMEOUT = 60.0


def _probe_limit(timeout: float | None) -> float:
    return _PROBE_TIMEOUT if timeout is None else min(timeout, _PROBE_TIMEOUT)


def _probe_wait_timeout(binary: str, limit: float) -> QsvTimeout:
    return QsvTimeout(
        f"timed out after {limit}s waiting for another client's --capabilities probe",
        exit_code=EXIT_TIMEOUT,
        args=[binary, "--capabilities"],
    )


def _caps_key(binary: str) -> _CapsKey | None:
    try:
        st = os.stat(binary)
    except OSError:
        return None
    return (binary, st.st_size, st.st_mtime_ns)


def _private_copy(caps: Capabilities) -> Capabilities:
    """A copy of cached capabilities whose dicts no other client shares, so one caller
    mutating ``raw`` or ``feature_versions`` cannot change what other clients see."""
    return replace(caps, feature_versions=dict(caps.feature_versions), raw=copy.deepcopy(caps.raw))


def _sync_probe_lock(key: _CapsKey) -> threading.Lock:
    with _probe_locks_guard:
        return _sync_probe_locks.setdefault(key, threading.Lock())


def _async_probe_lock(key: _CapsKey) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _probe_locks_guard:
        locks = _async_probe_locks.get(loop)
        if locks is None:
            locks = _async_probe_locks[loop] = weakref.WeakValueDictionary()
        lock = locks.get(key)
        if lock is None:
            lock = locks[key] = asyncio.Lock()
        return lock


def _ensure_json_format(args: Sequence[Arg]) -> list[Arg]:
    out = list(args)
    if not any(str(a) == "--format" or str(a).startswith("--format=") for a in out):
        out += ["--format", "json"]
    return out


class Qsv(_Base):
    """Blocking qsv client.

    >>> qsv = Qsv(timeout=600)
    >>> qsv.count("data.csv")
    1000
    >>> rows = qsv.run("stats", "data.csv", "--everything").csv_rows()
    """

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
        """Run ``qsv <command> <args...>``.

        Args:
            stdin: data to feed on stdin. Default: stdin is closed (``/dev/null``).
            stdout_path: stream stdout to this file instead of holding it in memory (use for
                large outputs). The file is created/truncated, parent dirs included. A
                relative path is resolved against the client's ``cwd``, if set.
            timeout: overrides the client default for this run. It limits each qsv process
                separately: a client's first run checks ``--capabilities`` first (bounded by
                the same timeout, at most 60s), so that run can take up to about twice as
                long.
            env: extra environment variables for this run.
            check: raise a :class:`QsvError` subclass if qsv fails (exit codes 0 and 255,
                qsv's "warning" code, count as success).
            text: decode stdout as UTF-8 (``result.stdout``); False keeps bytes
                (``result.stdout_bytes``). Use False for binary output (e.g. ``snappy
                compress``), which would otherwise raise ``UnicodeDecodeError``.

        Raises:
            QsvError: (subclass by error kind) when ``check`` and the run failed.
            QsvTimeout: the run exceeded its timeout; it and its children were killed.
        """
        self._check_version_once(timeout)
        return self._run_unchecked(
            command,
            args,
            stdin=stdin,
            stdout_path=stdout_path,
            timeout=timeout,
            env=env,
            check=check,
            text=text,
        )

    def _run_unchecked(
        self,
        command: str,
        args: Sequence[Arg],
        *,
        stdin: bytes | str | None = None,
        stdout_path: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        text: bool = True,
    ) -> QsvResult:
        argv = self._argv(command, args)
        effective_timeout = timeout if timeout is not None else self.timeout
        path, fh = _open_stdout(stdout_path, self.cwd)
        started = time.monotonic()
        try:
            raw = _process.run_sync(
                argv,
                env=self._build_env(env),
                cwd=self.cwd,
                stdin=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
                stdout_file=fh,
                timeout=effective_timeout,
                kill_grace=self.kill_grace,
            )
        except FileNotFoundError as exc:
            # also raised for a missing cwd; only a vanished binary is QsvNotFound
            if os.path.exists(self.binary):
                raise
            # errno form, so it reads like the FileNotFoundError it replaces (e.g. via qsv-clientd)
            raise QsvNotFound(errno.ENOENT, "qsv binary no longer exists", self.binary) from exc
        finally:
            if fh is not None:
                fh.close()
        return self._finish(
            raw,
            argv=argv,
            command=command,
            started=started,
            timeout=effective_timeout,
            text=text,
            stdout_path=path,
            check=check,
        )

    # -- capabilities -----------------------------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        """The binary's version, features and commands.

        Probed once per binary per process and shared by all clients, so ``raw`` values that
        depend on the environment (``max_jobs``) reflect the client that probed first.
        """
        if self._capabilities is None:
            self._capabilities = self._probe()
        return self._capabilities

    @property
    def version(self) -> str:
        return self.capabilities.version

    def _probe(self, timeout: float | None = None) -> Capabilities:
        limit = _probe_limit(timeout if timeout is not None else self.timeout)
        key = _caps_key(self.binary)
        if key is None:
            caps = self._probe_uncached(limit)
        else:
            deadline = time.monotonic() + limit
            lock = _sync_probe_lock(key)
            if not lock.acquire(timeout=limit):
                raise _probe_wait_timeout(self.binary, limit)
            try:
                cached = _caps_cache.get(key)
                if cached is None:
                    remaining = max(deadline - time.monotonic(), 0.01)
                    cached = _caps_cache[key] = self._probe_uncached(remaining)
                caps = cached
            finally:
                lock.release()
        # checked here, so it holds however the cache gets filled
        self._check_min_version(caps)
        return _private_copy(caps)

    def _probe_uncached(self, timeout: float) -> Capabilities:
        res = self._run_unchecked("", ["--capabilities"], check=False, timeout=timeout)
        return _caps_from_probe(self.binary, res)

    def _check_version_once(self, timeout: float | None = None) -> None:
        if self._capabilities is None:
            self._capabilities = self._probe(timeout)

    # -- conveniences -----------------------------------------------------------------------

    def count(self, path: Arg, *args: Arg, **kw: Any) -> int:
        """Number of records (``qsv count``)."""
        out = self.run("count", path, *args, **kw)._text()
        return int(out.strip().split()[0].split(";")[0].replace(",", ""))

    def headers(self, path: Arg, *args: Arg, **kw: Any) -> list[str]:
        """Column names (``qsv headers --just-names``)."""
        out = self.run("headers", "--just-names", path, *args, **kw)._text()
        return [line for line in out.splitlines() if line]

    def stats(self, path: Arg, *args: Arg, **kw: Any) -> list[dict[str, str]]:
        """``qsv stats`` rows, one dict per column. Pass ``stdout_path=`` to keep the CSV."""
        return self.run("stats", path, *args, **kw).csv_rows()

    def frequency(self, path: Arg, *args: Arg, **kw: Any) -> list[dict[str, str]]:
        """``qsv frequency`` rows (field, value, count, percentage, ...)."""
        return self.run("frequency", path, *args, **kw).csv_rows()

    def describegpt(self, path: Arg, *args: Arg, **kw: Any) -> Any:
        """``qsv describegpt`` output parsed as JSON (``--format json`` is added if absent).

        Pass the key via ``Qsv(llm_api_key=...)``, not ``--api-key``, to keep it out of the
        process table.
        """
        return self.run("describegpt", path, *_ensure_json_format(args), **kw).json()


class AsyncQsv(_Base):
    """asyncio qsv client. Same API as :class:`Qsv`, with awaitable methods.

    Cancelling the awaiting task kills the run's whole process group.

    >>> qsv = AsyncQsv(timeout=600)
    >>> n = await qsv.count("data.csv")
    """

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
        """See :meth:`Qsv.run`."""
        await self._check_version_once(timeout)
        return await self._run_unchecked(
            command,
            args,
            stdin=stdin,
            stdout_path=stdout_path,
            timeout=timeout,
            env=env,
            check=check,
            text=text,
        )

    async def _run_unchecked(
        self,
        command: str,
        args: Sequence[Arg],
        *,
        stdin: bytes | str | None = None,
        stdout_path: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        text: bool = True,
    ) -> QsvResult:
        argv = self._argv(command, args)
        effective_timeout = timeout if timeout is not None else self.timeout
        path, fh = _open_stdout(stdout_path, self.cwd)
        started = time.monotonic()
        try:
            raw = await _process.run_async(
                argv,
                env=self._build_env(env),
                cwd=self.cwd,
                stdin=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
                stdout_file=fh,
                timeout=effective_timeout,
                kill_grace=self.kill_grace,
            )
        except FileNotFoundError as exc:
            # also raised for a missing cwd; only a vanished binary is QsvNotFound
            if os.path.exists(self.binary):
                raise
            # errno form, so it reads like the FileNotFoundError it replaces (e.g. via qsv-clientd)
            raise QsvNotFound(errno.ENOENT, "qsv binary no longer exists", self.binary) from exc
        finally:
            if fh is not None:
                fh.close()
        return self._finish(
            raw,
            argv=argv,
            command=command,
            started=started,
            timeout=effective_timeout,
            text=text,
            stdout_path=path,
            check=check,
        )

    async def capabilities(self) -> Capabilities:
        """The binary's version, features and commands.

        Probed once per binary per process and shared by all clients, so ``raw`` values that
        depend on the environment (``max_jobs``) reflect the client that probed first.
        """
        if self._capabilities is None:
            self._capabilities = await self._probe()
        return self._capabilities

    async def version(self) -> str:
        return (await self.capabilities()).version

    async def _probe(self, timeout: float | None = None) -> Capabilities:
        limit = _probe_limit(timeout if timeout is not None else self.timeout)
        key = _caps_key(self.binary)
        if key is None:
            caps = await self._probe_uncached(limit)
        else:
            deadline = time.monotonic() + limit
            lock = _async_probe_lock(key)
            try:
                await asyncio.wait_for(lock.acquire(), limit)
            except asyncio.TimeoutError:
                raise _probe_wait_timeout(self.binary, limit) from None
            try:
                cached = _caps_cache.get(key)
                if cached is None:
                    remaining = max(deadline - time.monotonic(), 0.01)
                    cached = _caps_cache[key] = await self._probe_uncached(remaining)
                caps = cached
            finally:
                lock.release()
        self._check_min_version(caps)
        return _private_copy(caps)

    async def _probe_uncached(self, timeout: float) -> Capabilities:
        res = await self._run_unchecked("", ["--capabilities"], check=False, timeout=timeout)
        return _caps_from_probe(self.binary, res)

    async def _check_version_once(self, timeout: float | None = None) -> None:
        if self._capabilities is None:
            self._capabilities = await self._probe(timeout)

    async def count(self, path: Arg, *args: Arg, **kw: Any) -> int:
        out = (await self.run("count", path, *args, **kw))._text()
        return int(out.strip().split()[0].split(";")[0].replace(",", ""))

    async def headers(self, path: Arg, *args: Arg, **kw: Any) -> list[str]:
        out = (await self.run("headers", "--just-names", path, *args, **kw))._text()
        return [line for line in out.splitlines() if line]

    async def stats(self, path: Arg, *args: Arg, **kw: Any) -> list[dict[str, str]]:
        return (await self.run("stats", path, *args, **kw)).csv_rows()

    async def frequency(self, path: Arg, *args: Arg, **kw: Any) -> list[dict[str, str]]:
        return (await self.run("frequency", path, *args, **kw)).csv_rows()

    async def describegpt(self, path: Arg, *args: Arg, **kw: Any) -> Any:
        return (await self.run("describegpt", path, *_ensure_json_format(args), **kw)).json()
