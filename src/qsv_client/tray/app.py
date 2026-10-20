"""The pystray front end: turns the model's menu into a native tray menu.

The only module that imports pystray, and only inside functions, so swapping the toolkit is a
one-file job.
"""

from __future__ import annotations

import logging
import signal
import sys
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


def _prepare_macos() -> None:
    import AppKit
    import Foundation

    # a menu-bar-only app: no Dock icon, no app menu
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )
    # Python runs signal handlers between bytecodes on the main thread, which AppKit's run
    # loop never gives it; without this timer SIGTERM (and Ctrl-C) would wait forever
    Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, lambda timer: None)


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
        "qsv-clientd", icons.image(model.icon_state(state)), model.title(state), menu=menu
    )
    if sys.platform == "darwin":
        _prepare_macos()

    def redraw(state: TrayState) -> None:
        icon.icon = icons.image(model.icon_state(state))
        icon.title = model.title(state)
        icon.update_menu()

    def setup(icon: Any) -> None:
        icon.visible = True
        if not icon.HAS_MENU:
            log.warning(
                "this desktop shows tray icons without menus; on Linux, install the "
                "AppIndicator or GTK bindings for Python (see docs/DAEMON.md)"
            )
        controller.on_change = lambda state: _on_main_thread(lambda: redraw(state))
        controller.submit(model.START)
        controller.start_polling()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame: icon.stop())
    icon.run(setup=setup)
