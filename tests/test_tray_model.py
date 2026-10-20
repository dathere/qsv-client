"""The tray's menu, tooltip and icon state, derived from its state alone."""

from __future__ import annotations

from typing import Any

import pytest

from qsv_client.tray import model
from qsv_client.tray.model import SEPARATOR, MenuSpec, Mode, TrayState


def _status(**overrides: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "pid": 4242,
        "qsv": {"version": "25.1.0"},
        "jobs": {"running": 1, "max": 4},
        "datasets": 3,
        "pending_pairings": 0,
    }
    status.update(overrides)
    return status


def _texts(state: TrayState) -> list[str]:
    return ["—" if s.separator else s.text for s in model.build_menu(state)]


def test_stopped() -> None:
    state = TrayState()
    assert _texts(state) == ["qsv-clientd: stopped", "—", "Start", "—", "Quit"]
    assert model.build_menu(state)[0] == MenuSpec("qsv-clientd: stopped", enabled=False)
    assert model.build_menu(state)[2].action == model.START
    assert model.icon_state(state) == "stopped"
    assert model.title(state) == "qsv-clientd: stopped"


def test_hosted() -> None:
    state = TrayState(Mode.HOSTED, _status())
    assert _texts(state) == [
        "qsv-clientd: running",
        "qsv 25.1.0 · jobs 1/4 · 3 datasets",
        "—",
        "Stop",
        "Restart (unpairs open schematics)",
        "—",
        "Quit",
    ]
    assert [s.action for s in model.build_menu(state)] == [
        None,
        None,
        None,
        model.STOP,
        model.RESTART,
        None,
        model.QUIT,
    ]
    assert not model.build_menu(state)[1].enabled
    assert model.icon_state(state) == "running"
    assert model.title(state) == "qsv-clientd: running · qsv 25.1.0 · jobs 1/4 · 3 datasets"


def test_pending_pairings_need_attention() -> None:
    one = TrayState(Mode.HOSTED, _status(datasets=1, pending_pairings=1))
    assert model.detail_line(one) == "qsv 25.1.0 · jobs 1/4 · 1 dataset · 1 pairing request"
    assert model.icon_state(one) == "attention"
    two = TrayState(Mode.ATTACHED, _status(pending_pairings=2))
    assert model.detail_line(two) == "qsv 25.1.0 · jobs 1/4 · 3 datasets · 2 pairing requests"
    assert model.icon_state(two) == "attention"


def test_odd_status_fields_still_render() -> None:
    state = TrayState(Mode.HOSTED, {"pending_pairings": "x"})
    assert model.detail_line(state) == "qsv ? · jobs 0/? · 0 datasets"
    assert model.icon_state(state) == "running"


def test_attached_to_a_service_offers_the_takeover() -> None:
    state = TrayState(Mode.ATTACHED, _status(), service="launchd")
    assert _texts(state)[0] == "qsv-clientd: attached to pid 4242 (launchd service)"
    assert model.build_menu(state)[3] == MenuSpec("Stop service and run it here", model.TAKEOVER)


def test_attached_to_a_foreground_daemon_offers_nothing_to_stop() -> None:
    state = TrayState(Mode.ATTACHED, {})
    assert _texts(state) == [
        "qsv-clientd: attached to pid ?",
        "qsv ? · jobs 0/? · 0 datasets",
        "—",
        "Quit",
    ]


def test_starting_and_busy() -> None:
    starting = TrayState(Mode.STARTING)
    assert _texts(starting) == ["qsv-clientd: starting…", "—", "Quit"]
    assert model.icon_state(starting) == "busy"
    stopping = TrayState(Mode.HOSTED, _status(pending_pairings=1), busy="stopping…")
    assert _texts(stopping)[0] == "qsv-clientd: stopping…"
    assert model.STOP not in [s.action for s in model.build_menu(stopping)]
    assert model.icon_state(stopping) == "busy"


def test_error_offers_retry_and_wins_the_icon() -> None:
    state = TrayState(Mode.ERROR, _status(pending_pairings=3), error="no qsv found")
    assert _texts(state) == ["qsv-clientd: error: no qsv found", "—", "Retry", "—", "Quit"]
    assert model.icon_state(state) == "error"


@pytest.mark.parametrize("mode", list(Mode))
def test_every_menu_ends_with_quit(mode: Mode) -> None:
    menu = model.build_menu(TrayState(mode, _status()))
    assert menu[-2:] == [SEPARATOR, MenuSpec("Quit", model.QUIT)]
