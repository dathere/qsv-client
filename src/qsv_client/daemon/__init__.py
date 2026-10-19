"""qsv-clientd: a per-user daemon that runs qsv for local processes and Data Schematics.

Start it with ``qsv-clientd serve`` (or install it as a user service with
``qsv-clientd install``), then use :class:`DaemonClient` / :class:`AsyncDaemonClient`, which
take the same arguments as ``Qsv.run``. See docs/DAEMON.md for the protocol.
"""

from __future__ import annotations

from .client import AsyncDaemonClient, DaemonClient, qsv_or_daemon
from .config import API_VERSION, DEFAULT_PORT, DaemonConfig

__all__ = [
    "API_VERSION",
    "DEFAULT_PORT",
    "AsyncDaemonClient",
    "DaemonClient",
    "DaemonConfig",
    "qsv_or_daemon",
]
