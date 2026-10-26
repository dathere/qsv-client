"""Run the qsv CSV toolkit from Python.

Timeouts kill the whole process tree, failures raise typed exceptions, and capability
detection replaces parsing ``qsv --version``. Requires qsv 25.0.0 or later.
See https://github.com/dathere/qsv-client.
"""

from .capabilities import Capabilities
from .client import DEFAULT_BINARIES, MIN_QSV_VERSION, AsyncQsv, Qsv, QsvResult, find_qsv
from .errors import (
    QsvCsvError,
    QsvDaemonError,
    QsvDaemonUnavailable,
    QsvEncodingError,
    QsvError,
    QsvInferenceError,
    QsvIOError,
    QsvNetworkError,
    QsvNoMatch,
    QsvNotFound,
    QsvOutOfMemory,
    QsvTimeout,
    QsvUsageError,
    QsvVersionError,
    QsvWriteError,
)

__version__ = "0.6.0"

__all__ = [
    "DEFAULT_BINARIES",
    "MIN_QSV_VERSION",
    "AsyncQsv",
    "Capabilities",
    "Qsv",
    "QsvCsvError",
    "QsvDaemonError",
    "QsvDaemonUnavailable",
    "QsvEncodingError",
    "QsvError",
    "QsvIOError",
    "QsvInferenceError",
    "QsvNetworkError",
    "QsvNoMatch",
    "QsvNotFound",
    "QsvOutOfMemory",
    "QsvResult",
    "QsvTimeout",
    "QsvUsageError",
    "QsvVersionError",
    "QsvWriteError",
    "find_qsv",
]
