"""The :class:`Qsv` (blocking) and :class:`AsyncQsv` (asyncio) clients."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from . import _process
from .capabilities import Capabilities, from_capabilities_json, from_version_output, parse_version
from .errors import (
    EXIT_TIMEOUT,
    SUCCESS_EXIT_CODES,
    QsvNotFound,
    QsvTimeout,
    QsvVersionError,
    error_from_run,
)

#: binaries tried, in order, when none is given and ``QSV_BIN`` is unset
DEFAULT_BINARIES = ("qsv", "qsvmcp", "qsvdp", "qsvlite")

Arg = str | os.PathLike[str] | int | float


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
            if Path(cand).is_file():
                return str(Path(cand))
            continue
        found = shutil.which(cand)
        if found:
            return found
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
            inherit_env: start from ``os.environ`` (True) or from an empty environment.
            cwd: working directory for every run.
            llm_api_key / llm_base_url / llm_model: passed to ``describegpt`` as
                ``QSV_LLM_APIKEY`` / ``QSV_LLM_BASE_URL`` / ``QSV_LLM_MODEL`` environment
                variables, so the key never appears in the process table.
            min_version: raise :class:`QsvVersionError` on first use if the binary is older.
        """
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
        env.update(self._env)
        if extra:
            env.update(extra)
        # the whole point: failures come back as one parseable line. Harmless on qsv versions
        # that predate it (they ignore the variable and print text).
        env["QSV_ERROR_FORMAT"] = "json"
        return env

    def _argv(self, command: str, args: Sequence[Arg]) -> list[str]:
        argv = [self.binary]
        if command:
            argv.append(command)
        argv.extend(os.fspath(a) if isinstance(a, os.PathLike) else str(a) for a in args)
        return argv

    def _check_min_version(self, caps: Capabilities) -> None:
        if self.min_version and caps.version_tuple < parse_version(self.min_version):
            raise QsvVersionError(
                f"{self.binary} is qsv {caps.version}; at least {self.min_version} is required"
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
        captured = stdout_path is None
        result = QsvResult(
            args=tuple(argv),
            exit_code=raw.exit_code,
            stdout=raw.stdout.decode("utf-8") if captured and text else None,
            stdout_bytes=raw.stdout if captured and not text else None,
            stderr=stderr,
            duration=duration,
            stdout_path=stdout_path,
        )
        if check and not result.ok:
            raise error_from_run(
                exit_code=raw.exit_code,
                stderr=stderr,
                args=argv,
                command=command or None,
            )
        return result


def _open_stdout(
    stdout_path: str | os.PathLike[str] | None,
) -> tuple[Path | None, IO[bytes] | None]:
    if stdout_path is None:
        return None, None
    path = Path(stdout_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, path.open("wb")


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
                large outputs). The file is created/truncated, parent dirs included.
            timeout: overrides the client default for this run.
            env: extra environment variables for this run.
            check: raise a :class:`QsvError` subclass if qsv fails (exit codes 0 and 255,
                qsv's "warning" code, count as success).
            text: decode stdout as UTF-8 (``result.stdout``); False keeps bytes
                (``result.stdout_bytes``).

        Raises:
            QsvError: (subclass by error kind) when ``check`` and the run failed.
            QsvTimeout: the run exceeded its timeout; it and its children were killed.
        """
        self._check_version_once()
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
        path, fh = _open_stdout(stdout_path)
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
        """The binary's version, features and commands (cached after the first call)."""
        if self._capabilities is None:
            self._capabilities = self._probe()
        return self._capabilities

    @property
    def version(self) -> str:
        return self.capabilities.version

    def _probe(self) -> Capabilities:
        res = self._run_unchecked("", ["--capabilities"], check=False, timeout=60)
        if res.exit_code == 0 and res.stdout:
            try:
                return from_capabilities_json(res.stdout)
            except ValueError:
                pass
        ver = self._run_unchecked("", ["--version"], timeout=60)
        lst = self._run_unchecked("", ["--list"], check=False, timeout=60)
        return from_version_output(ver.stdout or "", lst.stdout or "")

    def _check_version_once(self) -> None:
        if self.min_version and self._capabilities is None:
            self._check_min_version(self.capabilities)

    # -- conveniences -----------------------------------------------------------------------

    def count(self, path: Arg, *args: Arg, **kw: Any) -> int:
        """Number of records (``qsv count``)."""
        out = self.run("count", path, *args, **kw).stdout or ""
        return int(out.strip().split()[0].replace(",", ""))

    def headers(self, path: Arg, *args: Arg, **kw: Any) -> list[str]:
        """Column names (``qsv headers --just-names``)."""
        out = self.run("headers", "--just-names", path, *args, **kw).stdout or ""
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
        await self._check_version_once()
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
        path, fh = _open_stdout(stdout_path)
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
        """The binary's version, features and commands (cached after the first call)."""
        if self._capabilities is None:
            self._capabilities = await self._probe()
        return self._capabilities

    async def version(self) -> str:
        return (await self.capabilities()).version

    async def _probe(self) -> Capabilities:
        res = await self._run_unchecked("", ["--capabilities"], check=False, timeout=60)
        if res.exit_code == 0 and res.stdout:
            try:
                return from_capabilities_json(res.stdout)
            except ValueError:
                pass
        ver = await self._run_unchecked("", ["--version"], timeout=60)
        lst = await self._run_unchecked("", ["--list"], check=False, timeout=60)
        return from_version_output(ver.stdout or "", lst.stdout or "")

    async def _check_version_once(self) -> None:
        if self.min_version and self._capabilities is None:
            self._check_min_version(await self.capabilities())

    async def count(self, path: Arg, *args: Arg, **kw: Any) -> int:
        out = (await self.run("count", path, *args, **kw)).stdout or ""
        return int(out.strip().split()[0].replace(",", ""))

    async def headers(self, path: Arg, *args: Arg, **kw: Any) -> list[str]:
        out = (await self.run("headers", "--just-names", path, *args, **kw)).stdout or ""
        return [line for line in out.splitlines() if line]

    async def stats(self, path: Arg, *args: Arg, **kw: Any) -> list[dict[str, str]]:
        return (await self.run("stats", path, *args, **kw)).csv_rows()

    async def frequency(self, path: Arg, *args: Arg, **kw: Any) -> list[dict[str, str]]:
        return (await self.run("frequency", path, *args, **kw)).csv_rows()

    async def describegpt(self, path: Arg, *args: Arg, **kw: Any) -> Any:
        return (await self.run("describegpt", path, *_ensure_json_format(args), **kw)).json()
