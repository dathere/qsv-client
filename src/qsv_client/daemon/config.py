"""Where qsv-clientd keeps its files, and its JSON configuration."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PORT = 14470
"""Loopback port for browsers (Data Schematics). qsv pro's API uses 14462."""

API_VERSION = 1


def _home_override() -> Path | None:
    # one directory for everything; used by tests and for running several daemons side by side
    home = os.environ.get("QSV_CLIENTD_HOME")
    return Path(home) if home else None


def config_dir() -> Path:
    if (home := _home_override()) is not None:
        return home
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "qsv-client"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "qsv-client"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "qsv-client"


def state_dir() -> Path:
    if (home := _home_override()) is not None:
        return home
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(local) / "qsv-client"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "qsv-client"
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "qsv-client"


def runtime_dir() -> Path:
    if (home := _home_override()) is not None:
        return home / "run"
    if sys.platform.startswith("linux") and (xdg := os.environ.get("XDG_RUNTIME_DIR")):
        return Path(xdg) / "qsv-client"
    return state_dir() / "run"


def default_socket_path() -> Path:
    return Path(os.environ.get("QSV_CLIENTD_SOCKET") or runtime_dir() / "qsvd.sock")


def socket_path_error(path: str | os.PathLike[str], exc: OSError) -> str | None:
    """The message for a socket path too long for AF_UNIX (about 104 bytes on macOS, 108 on
    Linux), or None when ``exc`` is some other error. CPython reports it without an errno."""
    if exc.errno is not None or "path too long" not in str(exc):
        return None
    return (
        f"the socket path {os.fspath(path)} is too long for a Unix socket "
        f"({len(os.fsencode(path))} bytes); set QSV_CLIENTD_SOCKET, or `socket` in "
        "daemon.json, to a shorter path"
    )


def local_token_path() -> Path:
    return runtime_dir() / "local-token"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make it readable by the current user only."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(0o700)
    return path


def write_private_file(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` with owner-only permissions."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# keyed by DaemonConfig's field annotations (strings, under `from __future__ import annotations`)
_TYPE_CHECKS: dict[str, Callable[[object], bool]] = {
    "str | None": lambda v: v is None or isinstance(v, str),
    "int": _is_int,
    "float": lambda v: _is_int(v) or isinstance(v, float),
    "bool": lambda v: isinstance(v, bool),
    "list[str]": lambda v: isinstance(v, list) and all(isinstance(s, str) for s in v),
}


@dataclass
class DaemonConfig:
    """Settings read from ``daemon.json`` in :func:`config_dir` (every key is optional)."""

    binary: str | None = None
    """qsv binary (or sandbox wrapper); default: ``$QSV_BIN``, then PATH, like ``Qsv``."""
    port: int = DEFAULT_PORT
    """Loopback TCP port for browsers (and for local clients on Windows). 0 disables it."""
    socket: str | None = None
    """Unix socket path (POSIX only); default ``<runtime dir>/qsvd.sock``."""
    max_jobs: int = max((os.cpu_count() or 2) // 2, 1)
    max_timeout: float = 3600.0
    """Upper bound on any local run's timeout, and the timeout when a request sets none."""
    inherit_env: bool = True
    kill_grace: float = 5.0
    browser: bool = True
    """Allow paired Data Schematics to use the daemon."""
    browser_origins: list[str] = field(default_factory=lambda: ["null"])
    """Origins allowed to pair. ``null`` is what a ``file://`` page sends."""
    browser_timeout: float = 60.0
    browser_max_output: int = 4 * 1024 * 1024
    # one stored result a page can read page by page (see results.py)
    browser_max_result: int = 1 << 30
    browser_max_render: int = 64 * 1024 * 1024
    token_ttl: float = 8 * 3600.0
    allow_fetch: bool = True
    """Let paired schematics ask the daemon to download the latest data (Phase D)."""

    @classmethod
    def load(cls, path: Path | None = None) -> DaemonConfig:
        path = path or config_dir() / "daemon.json"
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise ValueError(f"{path}: unknown setting(s): {', '.join(unknown)}")
        for name, value in data.items():
            expected = str(known[name].type)
            if not _TYPE_CHECKS[expected](value):
                raise ValueError(f"{path}: {name} must be {expected}, not {value!r}")
        return cls(**data)

    def socket_path(self) -> Path:
        return Path(self.socket) if self.socket else default_socket_path()
