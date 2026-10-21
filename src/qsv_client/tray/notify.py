"""Desktop notifications for the tray, through whichever backend this desktop has.

1. pystray's own, where its backend has them (macOS through osascript, which macOS shows as
   coming from "Script Editor"; Windows; GTK and AppIndicator on Linux).
2. On Linux with pystray's plain X fallback, ``notify-send`` if it is installed.
3. Otherwise none: the icon's dot and tooltip still show waiting pairing requests.

A notification only informs. Approving always takes the menu, with the code in view.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

log = logging.getLogger("qsv_client.tray")

Notifier = Callable[[str, str], None]
"""Show (title, message). Never raises: a failed notification is only logged."""


def _guarded(backend: str, send: Notifier) -> Notifier:
    def notify(title: str, message: str) -> None:
        try:
            send(title, message)
        except Exception:
            log.warning("a notification through %s failed", backend, exc_info=True)

    return notify


def nothing(title: str, message: str) -> None:
    pass


def choose(icon: Any) -> Notifier:
    if getattr(icon, "HAS_NOTIFICATION", False):
        return _guarded("pystray", lambda title, message: icon.notify(message, title))
    if sys.platform.startswith("linux") and (exe := shutil.which("notify-send")):

        def notify_send(title: str, message: str) -> None:
            subprocess.run(
                [exe, "--app-name=qsv-clientd", title, message],
                check=True,
                capture_output=True,
                timeout=10,
            )

        return _guarded("notify-send", notify_send)
    log.info("no desktop notifications here; the icon's dot shows pairing requests")
    return nothing
