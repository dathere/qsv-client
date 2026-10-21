"""What a pending pairing request says to the user, shared by ``qsv-clientd approve`` and the
tray so the two can't drift."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

NO_DATASET = (
    "This schematic matches no registered dataset. Register it first:\n"
    "  qsv-clientd register <data.csv> --schematic <schematic.html>"
)
FILE_ORIGIN = "Origin: null (a page opened from a file, or a sandboxed frame)."


@dataclass(frozen=True)
class PairingText:
    code: str
    title: str | None
    """The dataset's title; None when the schematic matches no registered dataset."""
    path: str | None
    fetched_copy_of: str | None
    """The original file, when the dataset is a fetched copy of it."""
    origin: str
    warning: str | None
    """Set when the request didn't come from a local file (origin ``null``)."""

    @property
    def can_approve(self) -> bool:
        return self.title is not None

    @property
    def headline(self) -> str:
        return f"Pair code {self.code} for {self.title!r} ({self.path})"

    @property
    def fetched_note(self) -> str | None:
        if self.fetched_copy_of is None:
            return None
        return f"(the fetched copy of {self.fetched_copy_of}, which is not being read)"

    @property
    def origin_line(self) -> str:
        return self.warning or FILE_ORIGIN


def dataset_title(dataset: Mapping[str, Any]) -> str:
    """A dataset's title, else its file's name (a title is optional when registering)."""
    return str(dataset.get("title") or PurePath(str(dataset["path"])).name)


def describe_pairing(item: Mapping[str, Any]) -> PairingText:
    """Describe one ``GET /v1/pairings`` item."""
    ds = item.get("dataset")
    origin = str(item.get("origin"))
    return PairingText(
        code=str(item["code"]),
        title=dataset_title(ds) if ds else None,
        path=str(ds["path"]) if ds else None,
        fetched_copy_of=(ds.get("original_path") or None) if ds else None,
        origin=origin,
        warning=None
        if origin == "null"
        else f"WARNING: the request came from {origin!r}, not a local file.",
    )
