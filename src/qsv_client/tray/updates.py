"""The tray's opt-in check for a newer qsv release ("Check for qsv updates" in its menu).

Off by default. When on, the tray asks GitHub's API for qsv's latest release about once a day:
one HTTPS GET with nothing about the user's data, and nothing sent anywhere else.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..capabilities import parse_version
from ..daemon.config import config_dir, write_private_file

log = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/dathere/qsv/releases/latest"
RELEASES_PAGE = "https://github.com/dathere/qsv/releases/latest"
"""What the menu opens: a constant, never a URL from the API's reply."""

INTERVAL = 24 * 3600.0
"""Seconds between checks while the tray runs."""
RETRY_INTERVAL = 3600.0
"""Seconds before trying again after a check failed (offline at login, say)."""

SETTINGS_FILE = "tray.json"
_TIMEOUT = 10.0
_MAX_BYTES = 1 << 20  # the reply is a few KB; release notes make it larger, never this large

Version = tuple[int, int, int]


@dataclass(frozen=True)
class Settings:
    check_updates: bool = False
    notified: str | None = None
    """The newest qsv a notification was shown for, so each release is announced once."""


def settings_path() -> Path:
    return config_dir() / SETTINGS_FILE


def load_settings() -> Settings:
    """The saved settings; the defaults when there are none or they can't be read."""
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError):
        log.warning("couldn't read %s; using the defaults", path, exc_info=True)
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    notified = data.get("notified_version")
    return Settings(
        check_updates=data.get("check_qsv_updates") is True,
        notified=notified if isinstance(notified, str) else None,
    )


def save_settings(settings: Settings) -> None:
    """Raises OSError if the file can't be written."""
    data = {"check_qsv_updates": settings.check_updates, "notified_version": settings.notified}
    write_private_file(settings_path(), (json.dumps(data, indent=2) + "\n").encode())


def latest_release() -> Version:
    """qsv's latest release, from GitHub. Raises OSError or ValueError."""
    req = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"qsv-client/{__version__}",
        },
    )
    try:
        with urllib.request.build_opener().open(req, timeout=_TIMEOUT) as resp:
            body = resp.read(_MAX_BYTES + 1)
    except http.client.HTTPException as exc:  # not an OSError: a malformed or cut-off reply
        raise OSError(str(exc)) from None
    if len(body) > _MAX_BYTES:
        raise ValueError(f"{RELEASES_API} answered with more than {_MAX_BYTES} bytes")
    data = json.loads(body)
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str):
        raise ValueError(f"{RELEASES_API} answered without a tag_name")
    return parse_version(tag.removeprefix("v"))
