"""Logging setup for qsv-clientd: stderr, and optionally a rotating log file."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FORMAT = "%(asctime)s qsv-clientd %(levelname)s %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3


def configure_logging(level: int, file: Path | None = None) -> list[logging.Handler]:
    """Send log records at ``level`` and above to stderr and, if given, to ``file``.

    stderr is skipped when there is none (``pythonw``, a gui-script). Returns the handlers
    added to the root logger, so a caller can remove and close them.
    """
    handlers: list[logging.Handler] = []
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
            )
        )
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    return handlers
