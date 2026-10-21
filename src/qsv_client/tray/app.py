"""The pystray front end: turns the model's menu into a native tray menu.

The only module that imports pystray, and only inside functions, so swapping the toolkit is a
one-file job.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Callable
from typing import Any

from . import model
from .controller import Controller
from .model import MenuSpec, TrayState

log = logging.getLogger("qsv_client.tray")


def _on_main_thread(fn: Callable[[], None]) -> None:
    """Run ``fn`` on the GUI thread. pystray's macOS backend changes AppKit objects directly,
    and AppKit isn't thread-safe; the other backends already hand their changes over."""
    if sys.platform == "darwin":
        from PyObjCTools import AppHelper

        AppHelper.callAfter(fn)
    else:
        fn()


def _hide_dock_icon() -> None:
    # a menu-bar-only app: no Dock icon, no app menu
    import AppKit

    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )


_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_GLIB_BACKENDS = ("pystray._gtk", "pystray._appindicator")


def _handle_signals_before_run(icon: Any) -> None:
    """Windows and xorg: the GUI loop runs Python code, so Python's own handlers work."""
    if sys.platform == "darwin" or type(icon).__module__ in _GLIB_BACKENDS:
        return
    for sig in _SIGNALS:
        signal.signal(sig, lambda signum, frame: icon.stop())


def _handle_signals_in_loop(icon: Any) -> None:
    """macOS and GTK: called from setup, once the backend has set up its own SIGINT handling.

    Their native loops never give Python's signal handlers a turn, and their own SIGINT
    handling (NSApp terminate on macOS, SIG_DFL on GTK) ends the process without letting
    the tray stop its daemon. Register with the loop itself instead, so a signal stops the
    icon and run() returns to the caller's cleanup.
    """
    if sys.platform == "darwin":
        from PyObjCTools import AppHelper, MachSignals

        def install() -> None:
            # on the main thread, inside the run loop: after pystray installed its own
            for sig in _SIGNALS:
                MachSignals.signal(sig, lambda signum: icon.stop())

        AppHelper.callAfter(install)
    elif type(icon).__module__ in _GLIB_BACKENDS:
        from gi.repository import GLib

        def stop() -> bool:
            icon.stop()
            return False  # remove the source

        for sig in _SIGNALS:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, stop)


_WINDOWS_TOOLTIP = 127
"""UTF-16 code units in a Windows tooltip (NOTIFYICONDATAW.szTip is 128 WCHARs)."""


def _tooltip(text: str) -> str:
    if sys.platform != "win32" or len(text.encode("utf-16-le")) // 2 <= _WINDOWS_TOOLTIP:
        return text
    while len((text + "…").encode("utf-16-le")) // 2 > _WINDOWS_TOOLTIP:
        text = text[:-1]
    return text + "…"


def _rebuild_windows_menu_before_it_opens(icon: Any, stale: threading.Event) -> None:
    """Windows: rebuild the menu on the GUI thread, just before it pops up.

    pystray's Windows update_menu() destroys the native menu, which must not happen on
    another thread, or while the menu is open; so state changes only mark it stale.
    """
    from pystray._util import win32

    on_notify = icon._message_handlers[win32.WM_NOTIFY]

    def notify(wparam: Any, lparam: Any) -> Any:
        if lparam == win32.WM_RBUTTONUP and stale.is_set():
            stale.clear()
            icon.update_menu()
        return on_notify(wparam, lparam)

    icon._message_handlers[win32.WM_NOTIFY] = notify


def _item(pystray: Any, spec: MenuSpec, controller: Controller) -> Any:
    if spec.separator:
        return pystray.Menu.SEPARATOR
    action = spec.action
    if action is None:
        return pystray.MenuItem(spec.text, None, enabled=False)
    if action == model.QUIT:
        # run() returns once the icon stops; the caller then shuts the controller down
        return pystray.MenuItem(spec.text, lambda icon, item: icon.stop())
    return pystray.MenuItem(
        spec.text, lambda icon, item: controller.submit(action), enabled=spec.enabled
    )


def check() -> None:
    """Raise ImportError unless the tray extra (pystray and Pillow) is installed."""
    import pystray  # noqa: F401

    from . import icons  # noqa: F401


def run(controller: Controller) -> None:
    """Show the tray icon and run the GUI loop on this (the main) thread until Quit."""
    import pystray

    from . import icons

    state = controller.state
    menu = pystray.Menu(
        lambda: tuple(_item(pystray, s, controller) for s in model.build_menu(controller.state))
    )
    icon = pystray.Icon(
        "qsv-clientd",
        icons.image(model.icon_state(state)),
        _tooltip(model.title(state)),
        menu=menu,
    )
    stale_menu = threading.Event()
    if sys.platform == "darwin":
        _hide_dock_icon()
    elif sys.platform == "win32":
        _rebuild_windows_menu_before_it_opens(icon, stale_menu)

    def redraw(state: TrayState) -> None:
        icon.icon = icons.image(model.icon_state(state))
        icon.title = _tooltip(model.title(state))
        if sys.platform == "win32":
            stale_menu.set()
        else:
            icon.update_menu()

    def setup(icon: Any) -> None:
        icon.visible = True
        if not icon.HAS_MENU:
            log.warning(
                "this desktop shows tray icons without menus; on Linux, install the "
                "AppIndicator or GTK bindings for Python (see docs/DAEMON.md)"
            )
        _handle_signals_in_loop(icon)
        controller.on_change = lambda state: _on_main_thread(lambda: redraw(state))
        controller.submit(model.START)
        controller.start_polling()

    _handle_signals_before_run(icon)
    icon.run(setup=setup)
