from __future__ import annotations

import json

import pytest

from qsv_client import (
    QsvCsvError,
    QsvError,
    QsvNetworkError,
    QsvNoMatch,
    QsvUsageError,
    QsvWriteError,
)
from qsv_client.errors import error_from_run, parse_json_error


def _line(kind: str, **extra: object) -> str:
    err = {
        "kind": kind,
        "level": "error",
        "message": f"{kind} happened",
        "exit_code": 1,
        "command": "select",
        "qsv_version": "24.0.0",
        **extra,
    }
    return json.dumps({"error": err})


@pytest.mark.parametrize(
    ("kind", "cls"),
    [
        ("csv", QsvCsvError),
        ("usage", QsvUsageError),
        ("no_match", QsvNoMatch),
        ("network", QsvNetworkError),
        ("write", QsvWriteError),
    ],
)
def test_json_kind_maps_to_class(kind: str, cls: type[QsvError]) -> None:
    exc = error_from_run(exit_code=1, stderr=_line(kind), args=["qsv", "select"], command="x")
    assert type(exc) is cls
    assert exc.kind == kind
    assert exc.structured
    assert exc.message == f"{kind} happened"
    assert exc.command == "select"  # the JSON line wins over the caller's guess
    assert exc.args_run == ("qsv", "select")


def test_unknown_json_kind_is_base_class() -> None:
    exc = error_from_run(exit_code=1, stderr=_line("brand_new"), args=[], command=None)
    assert type(exc) is QsvError
    assert exc.kind == "brand_new"


def test_json_line_is_last_line_after_warnings() -> None:
    stderr = "some warning\nanother\n" + _line("csv") + "\n"
    assert parse_json_error(stderr) is not None
    assert type(error_from_run(exit_code=1, stderr=stderr, args=[], command=None)) is QsvCsvError


def test_text_after_json_line_means_no_structured_error() -> None:
    assert parse_json_error(_line("csv") + "\ntrailing text\n") is None


@pytest.mark.parametrize("stderr", ["", "csv error: bad", "{not json", '{"other": 1}'])
def test_no_json_error(stderr: str) -> None:
    assert parse_json_error(stderr) is None


@pytest.mark.parametrize(
    ("code", "cls", "kind"),
    [(2, QsvUsageError, "usage"), (3, QsvNetworkError, "network"), (1, QsvError, "unknown")],
)
def test_fallback_to_exit_code(code: int, cls: type[QsvError], kind: str) -> None:
    exc = error_from_run(exit_code=code, stderr="something broke\n", args=[], command="count")
    assert type(exc) is cls
    assert exc.kind == kind
    assert not exc.structured
    assert exc.message == "something broke"


def test_fallback_usage_message_is_first_line() -> None:
    stderr = "Unknown flag: '--bogus'\n\nUsage:\n    qsv stats [options]\n    qsv stats --help\n"
    exc = error_from_run(exit_code=2, stderr=stderr, args=[], command="stats")
    assert exc.message == "Unknown flag: '--bogus'"


def test_fallback_empty_stderr_still_explains() -> None:
    exc = error_from_run(exit_code=1, stderr="", args=[], command="search")
    assert exc.message == "qsv exited with code 1"
    assert "search" in str(exc)
