"""The core and the tray's own logic must import without the tray extra (pystray, Pillow)."""

from __future__ import annotations

import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import importlib, importlib.abc, sys

    BLOCKED = ("pystray", "PIL")

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(f"blocked: {name}")
            return None

    sys.meta_path.insert(0, Block())
    for module in (
        "qsv_client",
        "qsv_client.daemon",
        "qsv_client.daemon.__main__",
        "qsv_client.daemon.host",
        "qsv_client.daemon.server",
        "qsv_client.tray",
        "qsv_client.tray.model",
        "qsv_client.tray.controller",
        "qsv_client.tray.notify",
        "qsv_client.daemon._text",
        "qsv_client.tray.app",
        "qsv_client.tray._lock",
        "qsv_client.tray.__main__",
    ):
        importlib.import_module(module)
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
    assert not loaded, loaded
    try:
        importlib.import_module("qsv_client.tray.icons")
    except ImportError:
        pass
    else:
        raise AssertionError("icons imported without Pillow")
    print("ok")
    """
)


def test_tray_logic_imports_without_the_extra() -> None:
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
