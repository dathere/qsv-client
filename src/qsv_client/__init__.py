"""Run the qsv CSV toolkit from Python.

Timeouts kill the whole process tree, failures raise typed exceptions, and capability
detection replaces parsing ``qsv --version``. See https://github.com/dathere/qsv-client.
"""

from .capabilities import Capabilities
from .client import DEFAULT_BINARIES, AsyncQsv, Qsv, QsvResult, find_qsv
from .errors import (
    QsvCsvError,
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

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BINARIES",
    "AsyncQsv",
    "Capabilities",
    "Qsv",
    "QsvCsvError",
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
