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


def _item(code: str = "ABC-123", origin: str = "null", **fields: Any) -> dict[str, Any]:
    dataset: Any = {"title": "Sales", "path": "/data/sales.csv"}
    item = {"code": code, "origin": origin, "dataset": dataset, "expires_in": 250}
    item.update(fields)
    return item


def _requests(n: int) -> tuple[model.Pending, ...]:
    return model.pending_requests(_item(f"AAA-00{i}") for i in range(n))


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
    one = TrayState(Mode.HOSTED, _status(datasets=1), pairings=_requests(1))
    assert model.detail_line(one) == "qsv 25.1.0 · jobs 1/4 · 1 dataset · 1 pairing request"
    assert model.icon_state(one) == "attention"
    two = TrayState(Mode.ATTACHED, _status(), pairings=_requests(2))
    assert model.detail_line(two) == "qsv 25.1.0 · jobs 1/4 · 3 datasets · 2 pairing requests"
    assert model.icon_state(two) == "attention"
    # the status counts approved requests whose page hasn't collected its token yet: no dot
    assert model.icon_state(TrayState(Mode.HOSTED, _status(pending_pairings=1))) == "running"


def test_odd_status_fields_still_render() -> None:
    state = TrayState(Mode.HOSTED, {})
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
    stopping = TrayState(Mode.HOSTED, _status(), busy="stopping…", pairings=_requests(1))
    assert _texts(stopping)[0] == "qsv-clientd: stopping…"
    assert model.STOP not in [s.action for s in model.build_menu(stopping)]
    assert model.icon_state(stopping) == "busy"


def test_error_offers_retry_and_wins_the_icon() -> None:
    state = TrayState(Mode.ERROR, _status(), error="no qsv found", pairings=_requests(3))
    assert _texts(state) == ["qsv-clientd: error: no qsv found", "—", "Retry", "—", "Quit"]
    assert model.icon_state(state) == "error"


@pytest.mark.parametrize("mode", list(Mode))
def test_every_menu_ends_with_quit(mode: Mode) -> None:
    menu = model.build_menu(TrayState(mode, _status()))
    assert menu[-2:] == [SEPARATOR, MenuSpec("Quit", model.QUIT)]


def _tree(spec: MenuSpec) -> Any:
    """A menu item as text, with its submenu's items nested under it."""
    if spec.separator:
        return "—"
    text = spec.text if spec.enabled else f"({spec.text})"
    if spec.action:
        text += f" -> {spec.action} {spec.arg}"
    return [text, [_tree(s) for s in spec.submenu]] if spec.submenu else text


def test_a_request_from_a_local_file() -> None:
    state = TrayState(Mode.HOSTED, _status(), pairings=model.pending_requests([_item()]))
    menu = model.build_menu(state)
    assert _tree(menu[2]) == "—" and _tree(menu[3]) == "(Waiting to pair:)"
    assert _tree(menu[4]) == [
        "ABC-123 · Sales",
        [
            "(For 'Sales')",
            "(/data/sales.csv)",
            "(Origin: null (a page opened from a file, or a sandboxed frame).)",
            "(Expires in 4m)",
            "—",
            "(Check that the page shows ABC-123)",
            "Approve -> approve ABC-123",
            "Deny -> deny ABC-123",
        ],
    ]


def test_a_request_from_a_website_needs_one_more_confirmation() -> None:
    item = _item(origin="https://evil.example", expires_in=30)
    item["dataset"]["original_path"] = "/data/orig.csv"
    (request,) = model.pending_requests([item])
    assert _tree(model.pairing_menu(request))[1] == [
        "(For 'Sales')",
        "(/data/sales.csv)",
        "((the fetched copy of /data/orig.csv, which is not being read))",
        "(WARNING: the request came from 'https://evil.example', not a local file.)",
        "(Expires in under a minute)",
        "—",
        "(Check that the page shows ABC-123)",
        ["Approve despite the warning", ["Yes, approve ABC-123 -> approve ABC-123"]],
        "Deny -> deny ABC-123",
    ]


def test_an_unregistered_schematic_can_only_be_denied() -> None:
    (request,) = model.pending_requests([_item(dataset=None, expires_in=None)])
    assert _tree(model.pairing_menu(request)) == [
        "ABC-123 · no registered dataset",
        [
            "(This schematic matches no registered dataset. Register it first:)",
            "(qsv-clientd register <data.csv> --schematic <schematic.html>)",
            "(Origin: null (a page opened from a file, or a sandboxed frame).)",
            "(Expires in 5 minutes or less)",
            "—",
            "Deny -> deny ABC-123",
        ],
    ]


def test_decided_requests_are_not_waiting() -> None:
    items = [_item("AAA-001", approved=True), _item("AAA-002", denied=True), _item("AAA-003")]
    assert [p.text.code for p in model.pending_requests(items)] == ["AAA-003"]


def test_requests_show_only_while_the_daemon_runs() -> None:
    stopped = TrayState(Mode.STOPPED, pairings=_requests(1))
    assert "(Waiting to pair:)" not in [_tree(s) for s in model.build_menu(stopped)]


def test_the_expiry_label_changes_once_a_minute() -> None:
    assert model.expiry_label(299) == model.expiry_label(240) == "expires in 4m"
    assert model.expiry_label(59) == "expires in under a minute"
