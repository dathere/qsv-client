"""The pystray front end, against a fake pystray (real tray icons need a desktop session).

Needs Pillow (the tray extra) for the icon images.
"""

from __future__ import annotations

import signal
import sys
import types
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import Any

import pytest

pytest.importorskip("PIL")

from qsv_client.tray import app, icons, model
from qsv_client.tray.controller import Controller
from qsv_client.tray.model import Mode, TrayState


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, items: Callable[[], tuple[Any, ...]]) -> None:
        self.items = items


class FakeMenuItem:
    def __init__(self, text: str, action: Any, enabled: bool = True) -> None:
        self.text, self.action, self.enabled = text, action, enabled


class FakeIcon:
    HAS_MENU = True
    last: FakeIcon | None = None

    def __init__(self, name: str, icon: Any, title: str, menu: FakeMenu) -> None:
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.visible = False
        self.stopped = False
        self.menu_updates = 0
        FakeIcon.last = self

    def run(self, setup: Callable[[FakeIcon], None]) -> None:
        setup(self)  # the real one then loops until stop()

    def stop(self) -> None:
        self.stopped = True

    def update_menu(self) -> None:
        self.menu_updates += 1

    def entries(self) -> list[Any]:
        return list(self.menu.items())

    def click(self, text: str) -> Any:
        (item,) = [i for i in self.entries() if getattr(i, "text", None) == text]
        return item.action(self, item)


class FakeController(Controller):
    """Records submissions instead of running them."""

    def __init__(self) -> None:
        super().__init__(make_daemon=lambda: pytest.fail("no daemon in app tests"))
        self.submitted: list[str] = []
        self.polling = False

    def submit(self, action: str) -> Future[None]:
        self.submitted.append(action)
        done: Future[None] = Future()
        done.set_result(None)
        return done

    def start_polling(self) -> None:
        self.polling = True


@pytest.fixture
def fake_pystray(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    module = types.ModuleType("pystray")
    module.Icon = FakeIcon  # type: ignore[attr-defined]
    module.Menu = FakeMenu  # type: ignore[attr-defined]
    module.MenuItem = FakeMenuItem  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pystray", module)
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    module.handlers = handlers  # type: ignore[attr-defined]
    FakeIcon.last = None
    yield module


@pytest.fixture
def controller() -> Iterator[FakeController]:
    c = FakeController()
    yield c
    c.shutdown()


def _run(controller: FakeController, platform: str, monkeypatch: pytest.MonkeyPatch) -> FakeIcon:
    monkeypatch.setattr(sys, "platform", platform)
    app.run(controller)
    assert FakeIcon.last is not None
    return FakeIcon.last


def test_run_shows_the_icon_and_starts(
    fake_pystray: types.ModuleType, controller: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    icon = _run(controller, "linux", monkeypatch)
    assert icon.visible and icon.name == "qsv-clientd"
    assert icon.icon is icons.image("stopped") and icon.title == "qsv-clientd: stopped"
    assert controller.submitted == [model.START] and controller.polling
    assert set(fake_pystray.handlers) == {signal.SIGINT, signal.SIGTERM}
    fake_pystray.handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert icon.stopped


def test_the_menu_follows_the_state(
    fake_pystray: types.ModuleType, controller: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    icon = _run(controller, "linux", monkeypatch)
    status = {"pid": 7, "qsv": {"version": "25.1.0"}, "jobs": {"running": 0, "max": 4}}
    controller._set(mode=Mode.HOSTED, status={**status, "pending_pairings": 1})
    assert icon.icon is icons.image("attention")
    assert icon.title.startswith("qsv-clientd: running · qsv 25.1.0")
    assert icon.menu_updates == 1
    entries = icon.entries()
    assert [getattr(e, "text", "—") for e in entries] == [
        "qsv-clientd: running",
        "qsv 25.1.0 · jobs 0/4 · 0 datasets · 1 pairing request",
        "—",
        "Stop",
        "Restart (unpairs open schematics)",
        "—",
        "Quit",
    ]
    assert entries[2] is FakeMenu.SEPARATOR
    assert not entries[0].enabled and entries[3].enabled
    icon.click("Restart (unpairs open schematics)")
    assert controller.submitted[-1] == model.RESTART
    icon.click("Quit")
    assert icon.stopped
    assert controller.submitted[-1] == model.RESTART  # Quit isn't a worker action


def test_macos_redraws_and_handles_signals_on_the_main_thread(
    fake_pystray: types.ModuleType, controller: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    appkit = types.ModuleType("AppKit")
    appkit.NSApplicationActivationPolicyAccessory = 1  # type: ignore[attr-defined]

    class NSApp:
        def setActivationPolicy_(self, policy: int) -> None:
            calls.append(("policy", policy))

    appkit.NSApplication = types.SimpleNamespace(sharedApplication=NSApp)  # type: ignore[attr-defined]
    helper = types.ModuleType("PyObjCTools.AppHelper")
    queued: list[Callable[[], None]] = []
    helper.callAfter = queued.append  # type: ignore[attr-defined]
    mach = types.ModuleType("PyObjCTools.MachSignals")
    mach_handlers: dict[int, Callable[[int], None]] = {}
    mach.signal = mach_handlers.__setitem__  # type: ignore[attr-defined]
    tools = types.ModuleType("PyObjCTools")
    tools.AppHelper = helper  # type: ignore[attr-defined]
    tools.MachSignals = mach  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "PyObjCTools", tools)
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", helper)
    monkeypatch.setitem(sys.modules, "PyObjCTools.MachSignals", mach)

    icon = _run(controller, "darwin", monkeypatch)
    assert calls == [("policy", 1)]  # no Dock icon
    assert fake_pystray.handlers == {}  # Python's handlers never run inside AppKit's loop
    # signals are registered on the main thread, after pystray installed its SIGINT handler
    install = queued.pop(0)
    install()
    assert set(mach_handlers) == {signal.SIGINT, signal.SIGTERM}
    controller._set(mode=Mode.ERROR, error="boom")
    assert icon.menu_updates == 0  # queued for the main thread, not done here
    (redraw,) = queued
    redraw()
    assert icon.icon is icons.image("error") and icon.menu_updates == 1
    mach_handlers[signal.SIGINT](signal.SIGINT)
    assert icon.stopped


class FakeGtkIcon(FakeIcon):
    pass


FakeGtkIcon.__module__ = "pystray._gtk"


def test_gtk_handles_signals_through_glib(
    fake_pystray: types.ModuleType, controller: FakeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources: dict[int, Callable[[], bool]] = {}
    glib = types.SimpleNamespace(
        PRIORITY_DEFAULT=0,
        unix_signal_add=lambda priority, sig, handler: sources.__setitem__(sig, handler),
    )
    repository = types.ModuleType("gi.repository")
    repository.GLib = glib  # type: ignore[attr-defined]
    gi = types.ModuleType("gi")
    gi.repository = repository  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    fake_pystray.Icon = FakeGtkIcon  # type: ignore[attr-defined]

    icon = _run(controller, "linux", monkeypatch)
    # pystray resets SIGINT to SIG_DFL before its loop: Python's handlers would be bypassed
    assert fake_pystray.handlers == {}
    assert set(sources) == {signal.SIGINT, signal.SIGTERM}
    assert sources[signal.SIGTERM]() is False  # a one-shot source
    assert icon.stopped
    controller._set(mode=Mode.HOSTED)
    assert icon.menu_updates == 1  # pystray's GTK backend hands it to the GTK loop itself


def test_a_desktop_without_menus_is_explained(
    fake_pystray: types.ModuleType,
    controller: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(FakeIcon, "HAS_MENU", False)
    _run(controller, "linux", monkeypatch)
    assert "tray icons without menus" in caplog.text


WM_NOTIFY, WM_LBUTTONUP, WM_RBUTTONUP = 0x40B, 0x202, 0x205


@pytest.fixture
def fake_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    win32 = types.ModuleType("pystray._util.win32")
    win32.WM_NOTIFY = WM_NOTIFY  # type: ignore[attr-defined]
    win32.WM_RBUTTONUP = WM_RBUTTONUP  # type: ignore[attr-defined]
    util = types.ModuleType("pystray._util")
    util.win32 = win32  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pystray._util", util)
    monkeypatch.setitem(sys.modules, "pystray._util.win32", win32)


def test_windows_rebuilds_the_menu_on_the_gui_thread_before_it_opens(
    fake_pystray: types.ModuleType,
    fake_win32: None,
    controller: FakeController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[int] = []
    monkeypatch.setattr(
        FakeIcon,
        "_message_handlers",
        {WM_NOTIFY: lambda wparam, lparam: notified.append(lparam)},
        raising=False,
    )
    icon = _run(controller, "win32", monkeypatch)
    assert set(fake_pystray.handlers) == {signal.SIGINT, signal.SIGTERM}
    for mode in (Mode.STARTING, Mode.HOSTED):
        controller._set(mode=mode)
        assert icon.title == model.title(TrayState(mode))
    assert icon.menu_updates == 0  # never from the worker thread
    handle = icon._message_handlers[WM_NOTIFY]  # type: ignore[attr-defined]
    handle(0, WM_LBUTTONUP)
    assert icon.menu_updates == 0  # a left click opens no menu
    handle(0, WM_RBUTTONUP)
    assert icon.menu_updates == 1  # rebuilt once, just before the popup
    handle(0, WM_RBUTTONUP)
    assert icon.menu_updates == 1  # nothing changed since
    assert notified == [WM_LBUTTONUP, WM_RBUTTONUP, WM_RBUTTONUP]  # pystray still handles it


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("short", "short"),
        ("x" * 127, "x" * 127),
        ("x" * 200, "x" * 126 + "…"),
        ("😀" * 100, "😀" * 63 + "…"),  # each emoji is two UTF-16 code units
    ],
)
def test_windows_tooltips_fit(text: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert app._tooltip(text) == expected
    monkeypatch.setattr(sys, "platform", "linux")
    assert app._tooltip(text) == text


def test_a_long_error_fits_the_windows_tooltip(
    fake_pystray: types.ModuleType,
    fake_win32: None,
    controller: FakeController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FakeIcon, "_message_handlers", {WM_NOTIFY: print}, raising=False)
    icon = _run(controller, "win32", monkeypatch)
    controller._set(mode=Mode.ERROR, error="qsv not found: " + "C:\\a\\long\\path;" * 20)
    assert len(icon.title) == 127 and icon.title.endswith("…")


def test_check_passes_with_the_extra(fake_pystray: types.ModuleType) -> None:
    app.check()
