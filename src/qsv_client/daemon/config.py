"""Where qsv-clientd keeps its files, and its TOML configuration."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path

from ..capabilities import parse_version

DEFAULT_PORT = 14470
"""Loopback port for browsers (Data Schematics). qsv pro's API uses 14462."""

API_VERSION = 1

LIVE_SCHEMATICS_QSV = "27.0.0"
"""The first qsv whose Data Schematics connect to qsv-clientd."""

PRIVATE_GET_CACHE_QSV = "27.0.0"
"""The first qsv that keeps its `get` cache readable by its owner only (on Unix)."""


def schematics_notice(qsv_version: object) -> str | None:
    """Why schematics won't go live with this qsv, or None when they will (or it can't tell)."""
    if not isinstance(qsv_version, str):
        return None
    try:
        older = parse_version(qsv_version) < parse_version(LIVE_SCHEMATICS_QSV)
    except ValueError:
        return None
    if not older:
        return None
    return (
        f"qsv {qsv_version} writes Data Schematics that can't connect to qsv-clientd. "
        f"Live schematics need qsv {LIVE_SCHEMATICS_QSV} or later."
    )


def _home_override() -> Path | None:
    # one directory for everything; used by tests and for running several daemons side by side
    home = os.environ.get("QSV_CLIENTD_HOME")
    return Path(home) if home else None


CONFIG_FILE = "daemon.toml"


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
        f"{CONFIG_FILE}, to a shorter path"
    )


def config_path() -> Path:
    """The default config file, ``daemon.toml`` in :func:`config_dir`."""
    return config_dir() / CONFIG_FILE


def local_token_path() -> Path:
    return runtime_dir() / "local-token"


def log_path() -> Path:
    """Where a hosted daemon (the tray) logs. On macOS a subdirectory of ~/Library/Logs, so
    it doesn't clash with the launchd service's ~/Library/Logs/qsv-clientd.log."""
    if _home_override() is None and sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "qsv-client" / "qsv-clientd.log"
    return state_dir() / "qsv-clientd.log"


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
    """Settings read from :func:`config_path` (every key is optional)."""

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
    fetch_max_bytes: int = 10 << 30
    """Largest download a fetch accepts; a bigger one is refused and deleted."""
    fetch_timeout: float = 1800.0
    """Upper bound, in seconds, on downloading one fetch."""

    @classmethod
    def load(cls, path: Path | None = None) -> DaemonConfig:
        path = path or config_path()
        if not path.exists():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            if path.suffix.lower() == ".json":  # a config from before 0.6.0
                raise ValueError(
                    f"{path}: the config is TOML now ({CONFIG_FILE}, one `key = value` per "
                    f"line), not JSON: {exc}"
                ) from None
            raise ValueError(f"{path}: {exc}") from None
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


# what config_template says about each setting, and an example for those without a default
_HELP: dict[str, tuple[str, str | None]] = {
    "binary": (
        "The qsv binary (or a sandbox wrapper): a path, or a name to look up on PATH.\n"
        "Default: $QSV_BIN, then qsv on PATH.",
        '"/usr/local/bin/qsv"',
    ),
    "port": (
        "Loopback TCP port for Data Schematics (and local clients on Windows).\n"
        "0 turns the browser listener off.",
        None,
    ),
    "socket": (
        "Unix socket path for local clients (macOS and Linux). Default: qsvd.sock in the\n"
        "runtime directory. It must fit a socket address: about 104 bytes on macOS, 108 on Linux.",
        '"/path/to/qsvd.sock"',
    ),
    "max_jobs": (
        "How many qsv runs at once, at least 1. Default: half your CPU cores.",
        None,
    ),
    "max_timeout": (
        "Seconds. The longest a local run may take, and its timeout when it sets none.",
        None,
    ),
    "inherit_env": (
        "true: qsv runs get the daemon's environment. false: they start from an empty one.",
        None,
    ),
    "kill_grace": (
        "Seconds between asking a timed-out run to stop and killing it.",
        None,
    ),
    "browser": ("true or false: let paired Data Schematics use the daemon.", None),
    "browser_origins": (
        'Origins allowed to pair, such as "https://example.org".\n'
        '"null" is what a schematic opened as a file (file://) sends.',
        None,
    ),
    "browser_timeout": ("Seconds. How long a qsv run started for a schematic may take.", None),
    "browser_max_output": (
        "Bytes. The most output a schematic gets back from one run (default 4 MiB).",
        None,
    ),
    "browser_max_result": (
        "Bytes. The largest result a schematic can read page by page (default 1 GiB).",
        None,
    ),
    "browser_max_render": (
        "Bytes. The largest re-rendered schematic sent back to a page (default 64 MiB).",
        None,
    ),
    "token_ttl": ("Seconds a schematic stays paired (default 8 hours).", None),
    "allow_fetch": (
        "true or false: let paired schematics ask the daemon to download the latest data.",
        None,
    ),
    "fetch_max_bytes": (
        "Bytes. The largest download a fetch accepts (default 10 GiB).",
        None,
    ),
    "fetch_timeout": ("Seconds. The longest one fetch's download may take.", None),
}


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)  # a string or a list of strings: valid TOML too


def config_template() -> str:
    """A ``daemon.toml`` with every setting commented out: its default (or an example) and
    the values it takes. Lines left commented keep following the daemon's own defaults."""
    defaults = DaemonConfig()
    lines = [
        "# qsv-clientd settings. Every setting is optional: one left commented out keeps its",
        '# default. To change one, delete the "# " before it, then restart the daemon.',
        "# More: https://github.com/dathere/qsv-client/blob/main/docs/DAEMON.md",
    ]
    for f in fields(DaemonConfig):
        help_text, example = _HELP[f.name]
        default = getattr(defaults, f.name)
        value = example if default is None else _toml_value(default)
        lines += ["", *(f"# {line}" for line in help_text.splitlines()), f"# {f.name} = {value}"]
    return "\n".join(lines) + "\n"
