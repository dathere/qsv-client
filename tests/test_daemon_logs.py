"""configure_logging: stderr and a rotating log file."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from qsv_client.daemon import logs

Configure = Callable[..., list[logging.Handler]]


@pytest.fixture
def configure() -> Iterator[Configure]:
    """configure_logging, undone afterwards (an open file handler would also block cleanup
    of tmp_path on Windows)."""
    root = logging.getLogger()
    saved_level = root.level
    added: list[logging.Handler] = []

    def call(level: int, file: Path | None = None) -> list[logging.Handler]:
        handlers = logs.configure_logging(level, file)
        added.extend(handlers)
        return handlers

    yield call
    for handler in added:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(saved_level)


def test_logs_to_a_rotating_file_and_stderr(configure: Configure, tmp_path: Path) -> None:
    path = tmp_path / "new" / "dir" / "qsv-clientd.log"
    file_handler, stderr_handler = configure(logging.INFO, path)
    assert isinstance(file_handler, logging.handlers.RotatingFileHandler)
    assert file_handler.maxBytes == logs.MAX_BYTES and file_handler.backupCount == logs.BACKUPS
    assert isinstance(stderr_handler, logging.StreamHandler)
    assert logging.getLogger().level == logging.INFO
    logging.getLogger("qsv_client.daemon").info("dataset registered: %s", "abc")
    file_handler.flush()
    line = path.read_text(encoding="utf-8")
    assert "qsv-clientd INFO dataset registered: abc" in line


def test_stderr_only_without_a_file(configure: Configure) -> None:
    (handler,) = configure(logging.WARNING)
    assert type(handler) is logging.StreamHandler
    assert logging.getLogger().level == logging.WARNING


def test_no_stderr_under_pythonw(
    configure: Configure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stderr", None)
    (handler,) = configure(logging.INFO, tmp_path / "d.log")
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert configure(logging.INFO) == []
