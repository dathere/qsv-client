"""The ``qsv-clientd-tray`` command (a gui-script: on Windows it has no console)."""

from __future__ import annotations

import argparse
import logging
import sys

from ..daemon import logs
from ..daemon.config import log_path, runtime_dir
from . import _lock, app
from .controller import Controller

log = logging.getLogger("qsv_client.tray")

TITLE = "qsv-clientd-tray"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TITLE, description="A system tray icon that runs and controls qsv-clientd."
    )
    p.add_argument("-v", "--verbose", action="store_true", help="log debug messages too")
    return p


def _message_box(text: str) -> None:
    """Tell the user, even without a console (pythonw on Windows has no stderr)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)  # MB_ICONERROR
    elif sys.stderr is not None:
        print(f"{TITLE}: {text}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ns = _parser().parse_args(argv)
    try:
        app.check()
    except ImportError as exc:
        _message_box(f"the tray needs pystray and Pillow: pip install 'qsv-client[tray]' ({exc})")
        return 1
    try:
        lock = _lock.acquire(runtime_dir() / "tray.lock")
    except _lock.AlreadyRunning as exc:
        _message_box(str(exc))
        return 1
    logs.configure_logging(logging.DEBUG if ns.verbose else logging.INFO, log_path())
    logging.getLogger("PIL").setLevel(logging.INFO)  # its debug lines are PNG chunk dumps
    controller = Controller()
    try:
        app.run(controller)
    except Exception as exc:
        log.exception("the tray failed")
        _message_box(f"the tray failed: {exc} (see {log_path()})")
        return 1
    finally:
        # Quit, a signal or a crash: stop a daemon the tray hosts, then let go of the lock
        controller.shutdown()
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
