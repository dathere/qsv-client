from __future__ import annotations

import os
import shutil
import stat
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from qsv_client import Qsv


def _real_qsv() -> str | None:
    return os.environ.get("QSV_BIN") or shutil.which("qsv")


@pytest.fixture(scope="session")
def qsv_bin() -> str:
    """A real qsv binary: $QSV_BIN, else `qsv` on PATH. Integration tests skip without one."""
    found = _real_qsv()
    if not found:
        pytest.skip("no qsv binary (set QSV_BIN or put qsv on PATH)")
    return found


@pytest.fixture(scope="session")
def qsv(qsv_bin: str) -> Qsv:
    return Qsv(qsv_bin, timeout=120)


@pytest.fixture
def csv_files(tmp_path: Path) -> Path:
    (tmp_path / "ok.csv").write_text("name,n\nalpha,1\nbeta,2\ngamma,2\n")
    (tmp_path / "ragged.csv").write_text("a,b\n1,2\n3\n")
    return tmp_path


@pytest.fixture
def fake_qsv(tmp_path: Path) -> Callable[[str], str]:
    """Write an executable stand-in for qsv running the given Python body; return its path."""
    if sys.platform == "win32":
        pytest.skip("fake executables use a shebang")

    def make(body: str) -> str:
        path = tmp_path / "fake-qsv"
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    return make
