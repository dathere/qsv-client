"""The ``qsv-clientd-tray`` command (a gui-script: on Windows it has no console)."""

from __future__ import annotations

import argparse
import functools
import logging
import sys

from ..daemon import logs
from ..daemon.config import log_path, runtime_dir
from . import _lock, app, autostart
from .controller import Controller, load_daemon

log = logging.getLogger("qsv_client.tray")

TITLE = "qsv-clientd-tray"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TITLE, description="A system tray icon that runs and controls qsv-clientd."
    )
    p.add_argument("-v", "--verbose", action="store_true", help="log debug messages too")
    p.add_argument("--binary", help="qsv binary to use (overrides daemon.json)")
    login = p.add_mutually_exclusive_group()
    login.add_argument(
        "--enable-autostart", action="store_true", help="start the tray at login, then exit"
    )
    login.add_argument(
        "--disable-autostart", action="store_true", help="stop starting it at login, then exit"
    )
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
    if ns.enable_autostart or ns.disable_autostart:
        # before the checks below: writing a login item needs neither the extra nor the lock
        try:
            done = autostart.enable() if ns.enable_autostart else autostart.disable()
        except Exception as exc:  # no qsv to pin, a bad daemon.json, a permission error
            _message_box(f"couldn't change starting at login: {exc}")
            return 1
        _message_box(done)
        return 0
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
    controller = Controller(make_daemon=functools.partial(load_daemon, ns.binary))
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
