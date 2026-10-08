"""Typed exceptions for failed qsv runs.

qsv releases after 24.0.0 support ``QSV_ERROR_FORMAT=json``, which ends a failed run with
one JSON line on stderr::

    {"error": {"kind": "csv", "level": "error", "message": "...", "exit_code": 1,
               "command": "select", "qsv_version": "24.0.0"}}

:func:`error_from_run` turns that line into the matching :class:`QsvError` subclass. For older
qsv versions, which only print free text, it falls back to qsv's documented exit codes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

# qsv's exit codes (src/clitypes.rs `QsvExitCode`)
EXIT_GOOD = 0
EXIT_BAD = 1
EXIT_INCORRECT_USAGE = 2
EXIT_NETWORK_ERROR = 3
EXIT_OUT_OF_MEMORY = 4
EXIT_ENCODING_ERROR = 5
EXIT_WARNING = 255
#: exit code reported for a run the client killed on timeout (as GNU ``timeout`` does)
EXIT_TIMEOUT = 124

#: exit codes qsv uses for a run that did what was asked (255 = warning, e.g. a broken pipe)
SUCCESS_EXIT_CODES = frozenset({EXIT_GOOD, EXIT_WARNING})


class QsvError(Exception):
    """A qsv run failed.

    Attributes:
        kind: qsv's error category (``usage``, ``csv``, ``io``, ``no_match``, ``network``,
            ``out_of_memory``, ``encoding``, ``inference``, ``write``, ``other``), or
            ``timeout`` for a run this client killed. ``unknown`` when an older qsv exited
            with code 1 and printed no structured error.
        message: the error message, without qsv's text-mode prefix (e.g. ``csv error: ``).
        exit_code: the process exit code.
        command: the qsv subcommand, when known.
        args: the full argv that was run (binary first).
        stderr: everything the process wrote to stderr.
        structured: True when the error came from qsv's JSON error line, False when it was
            inferred from the exit code and free-text stderr.
    """

    kind: str = "unknown"

    def __init__(
        self,
        message: str,
        *,
        kind: str | None = None,
        exit_code: int = EXIT_BAD,
        command: str | None = None,
        args: Sequence[str] = (),
        stderr: str = "",
        structured: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        if kind is not None:
            self.kind = kind
        self.exit_code = exit_code
        self.command = command
        self.args_run: tuple[str, ...] = tuple(args)
        self.stderr = stderr
        self.structured = structured

    def __str__(self) -> str:
        where = f"qsv {self.command}" if self.command else "qsv"
        return f"{where} failed ({self.kind}, exit {self.exit_code}): {self.message}"


class QsvUsageError(QsvError):
    """Bad flags or arguments (exit 2)."""

    kind = "usage"


class QsvCsvError(QsvError):
    """The input is not valid CSV, e.g. a ragged row."""

    kind = "csv"


class QsvIOError(QsvError):
    """A file could not be read or written."""

    kind = "io"


class QsvNoMatch(QsvError):
    """The command found nothing to report, e.g. ``search`` with no hits (exit 1)."""

    kind = "no_match"


class QsvNetworkError(QsvError):
    """A network operation failed (exit 3)."""

    kind = "network"


class QsvOutOfMemory(QsvError):
    """qsv refused or failed to run for lack of memory (exit 4)."""

    kind = "out_of_memory"


class QsvEncodingError(QsvError):
    """The input is not valid UTF-8 (exit 5)."""

    kind = "encoding"


class QsvInferenceError(QsvError):
    """An LLM call made by ``describegpt`` failed."""

    kind = "inference"


class QsvWriteError(QsvError):
    """qsv could not write its output, e.g. a full disk."""

    kind = "write"


class QsvTimeout(QsvError):
    """The run exceeded its timeout and this client killed it (and its children)."""

    kind = "timeout"


class QsvNotFound(Exception):
    """No qsv binary could be found."""


class QsvVersionError(Exception):
    """The qsv binary is older than the client was told to require."""


_KIND_TO_CLASS: dict[str, type[QsvError]] = {
    cls.kind: cls
    for cls in (
        QsvUsageError,
        QsvCsvError,
        QsvIOError,
        QsvNoMatch,
        QsvNetworkError,
        QsvOutOfMemory,
        QsvEncodingError,
        QsvInferenceError,
        QsvWriteError,
        QsvTimeout,
    )
}

_EXIT_CODE_TO_CLASS: dict[int, type[QsvError]] = {
    EXIT_INCORRECT_USAGE: QsvUsageError,
    EXIT_NETWORK_ERROR: QsvNetworkError,
    EXIT_OUT_OF_MEMORY: QsvOutOfMemory,
    EXIT_ENCODING_ERROR: QsvEncodingError,
}


def parse_json_error(stderr: str) -> dict[str, Any] | None:
    """Return the ``error`` object from qsv's JSON error line, or None if there isn't one.

    qsv prints it as the last stderr line. Earlier lines may be free-text warnings.
    """
    for line in reversed(stderr.splitlines()):
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            return None
        try:
            doc = json.loads(line)
        except ValueError:
            return None
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict) and isinstance(err.get("kind"), str):
            return err
        return None
    return None


def error_from_run(
    *,
    exit_code: int,
    stderr: str,
    args: Sequence[str],
    command: str | None,
) -> QsvError:
    """Build the exception for a run that exited with a failure code."""
    err = parse_json_error(stderr)
    if err is not None:
        kind = err["kind"]
        cls = _KIND_TO_CLASS.get(kind, QsvError)
        message = err.get("message")
        cmd = err.get("command")
        return cls(
            message if isinstance(message, str) else "",
            kind=kind,
            exit_code=exit_code,
            command=cmd if isinstance(cmd, str) else command,
            args=args,
            stderr=stderr,
            structured=True,
        )

    # older qsv: free text. The exit code still separates the main categories.
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        message = f"qsv exited with code {exit_code}"
    elif exit_code == EXIT_INCORRECT_USAGE:
        # docopt prints the problem first, then the command's usage block
        message = lines[0]
    else:
        # anything before the last line is usually a warning or progress output
        message = lines[-1]
    cls = _EXIT_CODE_TO_CLASS.get(exit_code, QsvError)
    return cls(message, exit_code=exit_code, command=command, args=args, stderr=stderr)
