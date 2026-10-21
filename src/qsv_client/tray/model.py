"""What the tray shows: its state, and the menu and icon derived from it. No GUI code."""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ..daemon._text import NO_DATASET, PairingText, describe_pairing

ServiceKind = Literal["launchd", "systemd"]
IconState = Literal["stopped", "running", "busy", "attention", "error"]

START = "start"
STOP = "stop"
RESTART = "restart"
TAKEOVER = "takeover"
"""Stop the installed service and host the daemon in the tray instead."""
RETRY = "retry"
QUIT = "quit"
APPROVE = "approve"
DENY = "deny"


class Mode(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    HOSTED = "hosted"
    """The tray runs the daemon itself."""
    ATTACHED = "attached"
    """Another process (a service, or ``qsv-clientd serve``) runs it; the tray only watches."""
    ERROR = "error"


@dataclass(frozen=True)
class TrayState:
    mode: Mode = Mode.STOPPED
    status: Mapping[str, Any] | None = None
    """The daemon's last ``GET /v1/status`` reply."""
    service: ServiceKind | None = None
    """The service ``qsv-clientd install`` set up, if any."""
    busy: str | None = None
    """What a running action is doing ("stopping…"); lifecycle actions wait for it."""
    error: str | None = None
    pairings: tuple[Pending, ...] = ()
    """Pairing requests waiting for the user's decision."""


@dataclass(frozen=True)
class Pending:
    """A pairing request, as the menu shows it. Compared by value: the expiry is a label that
    changes once a minute, so a waiting request doesn't rebuild an open menu every refresh."""

    text: PairingText
    expires: str


def expiry_label(seconds: object) -> str:
    if not isinstance(seconds, int):
        return "expires in 5 minutes or less"
    if seconds < 60:
        return "expires in under a minute"
    return f"expires in {seconds // 60}m"


def pending_requests(items: Iterable[Mapping[str, Any]]) -> tuple[Pending, ...]:
    """The ``GET /v1/pairings`` items still waiting for a decision (an approved request stays
    listed until its page collects the token)."""
    return tuple(
        Pending(describe_pairing(item), expiry_label(item.get("expires_in")))
        for item in items
        if not item.get("approved") and not item.get("denied")
    )


@dataclass(frozen=True)
class MenuSpec:
    text: str = ""
    action: str | None = None
    """The controller action this item runs; None makes it an information line."""
    enabled: bool = True
    separator: bool = False
    arg: str | None = None
    """The action's argument (a pairing code)."""
    submenu: tuple[MenuSpec, ...] = ()


SEPARATOR = MenuSpec(separator=True)


def _pending(state: TrayState) -> int:
    return len(state.pairings)


def status_line(state: TrayState) -> str:
    if state.busy:
        return f"qsv-clientd: {state.busy}"
    match state.mode:
        case Mode.HOSTED:
            return "qsv-clientd: running"
        case Mode.ATTACHED:
            pid = (state.status or {}).get("pid", "?")
            service = f" ({state.service} service)" if state.service else ""
            return f"qsv-clientd: attached to pid {pid}{service}"
        case Mode.STARTING:
            return "qsv-clientd: starting…"
        case Mode.ERROR:
            return f"qsv-clientd: error: {state.error}"
        case _:
            return "qsv-clientd: stopped"


def detail_line(state: TrayState) -> str | None:
    """qsv's version, jobs and datasets, while the daemon answers."""
    status = state.status
    if status is None or state.mode not in (Mode.HOSTED, Mode.ATTACHED):
        return None
    qsv = status.get("qsv") or {}
    jobs = status.get("jobs") or {}
    datasets = status.get("datasets", 0)
    parts = [
        f"qsv {qsv.get('version', '?')}",
        f"jobs {jobs.get('running', 0)}/{jobs.get('max', '?')}",
        f"{datasets} dataset" + ("" if datasets == 1 else "s"),
    ]
    if pending := _pending(state):
        parts.append(f"{pending} pairing request" + ("" if pending == 1 else "s"))
    return " · ".join(parts)


def title(state: TrayState) -> str:
    """The icon's tooltip."""
    return " · ".join(filter(None, [status_line(state), detail_line(state)]))


def _lifecycle(state: TrayState) -> list[MenuSpec]:
    if state.busy:
        return []
    match state.mode:
        case Mode.STOPPED:
            return [MenuSpec("Start", START)]
        case Mode.HOSTED:
            return [MenuSpec("Stop", STOP), MenuSpec("Restart (unpairs open schematics)", RESTART)]
        case Mode.ATTACHED if state.service:
            return [MenuSpec("Stop service and run it here", TAKEOVER)]
        case Mode.ERROR:
            return [MenuSpec("Retry", RETRY)]
        case _:
            # STARTING, or attached to a foreground `serve`: there is no route to stop it
            return []


def _info(text: str) -> MenuSpec:
    return MenuSpec(text, enabled=False)


def pairing_menu(request: Pending) -> MenuSpec:
    """One request: its details, then the decision. Approving takes the same checks as
    ``qsv-clientd approve``: no registered dataset, no approval; a page that isn't a local
    file (origin other than ``null``), one more confirmation."""
    t = request.text
    if t.can_approve:
        label = f"{t.code} · {t.title}"
        details = [_info(f"For {t.title!r}"), _info(str(t.path))]
        if t.fetched_note:
            details.append(_info(t.fetched_note))
    else:
        label = f"{t.code} · no registered dataset"
        details = [_info(line.strip()) for line in NO_DATASET.splitlines()]
    details += [_info(t.origin_line), _info(request.expires.capitalize())]
    decision = []
    if t.can_approve:
        decision.append(_info(f"Check that the page shows {t.code}"))
        if t.warning is None:
            decision.append(MenuSpec("Approve", APPROVE, arg=t.code))
        else:
            yes = MenuSpec(f"Yes, approve {t.code}", APPROVE, arg=t.code)
            decision.append(MenuSpec("Approve despite the warning", submenu=(yes,)))
    decision.append(MenuSpec("Deny", DENY, arg=t.code))
    return MenuSpec(label, submenu=(*details, SEPARATOR, *decision))


def build_menu(state: TrayState) -> list[MenuSpec]:
    items = [MenuSpec(status_line(state), enabled=False)]
    if detail := detail_line(state):
        items.append(MenuSpec(detail, enabled=False))
    if state.pairings and state.mode in (Mode.HOSTED, Mode.ATTACHED):
        items += [SEPARATOR, _info("Waiting to pair:")]
        items += [pairing_menu(p) for p in state.pairings]
    if lifecycle := _lifecycle(state):
        items += [SEPARATOR, *lifecycle]
    items += [SEPARATOR, MenuSpec("Quit", QUIT)]
    return items


def icon_state(state: TrayState) -> IconState:
    if state.mode is Mode.ERROR:
        return "error"
    if state.busy or state.mode is Mode.STARTING:
        return "busy"
    if state.mode in (Mode.HOSTED, Mode.ATTACHED):
        return "attention" if _pending(state) else "running"
    return "stopped"
