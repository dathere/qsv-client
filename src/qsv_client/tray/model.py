"""What the tray shows: its state, and the menu and icon derived from it. No GUI code."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

ServiceKind = Literal["launchd", "systemd"]
IconState = Literal["stopped", "running", "busy", "attention", "error"]

START = "start"
STOP = "stop"
RESTART = "restart"
TAKEOVER = "takeover"
"""Stop the installed service and host the daemon in the tray instead."""
RETRY = "retry"
QUIT = "quit"


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


@dataclass(frozen=True)
class MenuSpec:
    text: str = ""
    action: str | None = None
    """The controller action this item runs; None makes it an information line."""
    enabled: bool = True
    separator: bool = False


SEPARATOR = MenuSpec(separator=True)


def _pending(state: TrayState) -> int:
    count = (state.status or {}).get("pending_pairings")
    return count if isinstance(count, int) else 0


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


def build_menu(state: TrayState) -> list[MenuSpec]:
    items = [MenuSpec(status_line(state), enabled=False)]
    if detail := detail_line(state):
        items.append(MenuSpec(detail, enabled=False))
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
