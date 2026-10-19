"""Datasets the user has registered with qsv-clientd.

Browsers never name files: a paired Data Schematic refers to its dataset by id, and the
daemon supplies the path and the (user-approved) viz arguments itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import write_private_file

SCHEMATIC_SOURCE_ID = "qsv-schematic-source"
_SOURCE_RE = re.compile(
    r'<script\b[^>]*\bid="' + SCHEMATIC_SOURCE_ID + r'"[^>]*>(.*?)</script>', re.DOTALL
)

# viz flags whose value is a file the daemon controls (or that would let a page aim qsv at
# another file); registration rejects them so a re-render can only write the schematic
_FORBIDDEN_VIZ_FLAGS = ("-o", "--output")


@dataclass(frozen=True)
class Fingerprint:
    sha256: str
    size: int

    @classmethod
    def of(cls, path: Path) -> Fingerprint:
        h = hashlib.sha256()
        size = 0
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
                size += len(chunk)
        return cls(h.hexdigest(), size)


@dataclass(frozen=True)
class SchematicSource:
    """The ``qsv-schematic-source`` JSON that ``qsv viz smart`` embeds in a Data Schematic."""

    schematic_uuid: str | None
    fingerprint: Fingerprint | None
    viz_args: tuple[str, ...]
    pid: str | None
    qsv_version: str | None
    redacted: tuple[str, ...] = ()
    """viz flags whose file values were reduced to a basename; re-supply them to re-render."""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SchematicSource:
        fp = data.get("fingerprint")
        fingerprint = None
        if (
            isinstance(fp, dict)
            and fp.get("algo") == "sha256"
            and isinstance(fp.get("digest"), str)
            and isinstance(fp.get("bytes"), int)
        ):
            fingerprint = Fingerprint(fp["digest"].lower(), fp["bytes"])
        args = data.get("viz_args")
        return cls(
            schematic_uuid=_opt_str(data.get("schematic_uuid")),
            fingerprint=fingerprint,
            viz_args=tuple(a for a in args if isinstance(a, str)) if isinstance(args, list) else (),
            pid=_opt_str(data.get("pid")),
            qsv_version=_opt_str(data.get("qsv_version")),
            redacted=tuple(r for r in red if isinstance(r, str))
            if isinstance(red := data.get("redacted"), list)
            else (),
        )

    @classmethod
    def from_html(cls, html: str) -> SchematicSource | None:
        match = _SOURCE_RE.search(html)
        if match is None:
            return None
        try:
            data = json.loads(match.group(1))
        except ValueError:
            return None
        return cls.from_json(data) if isinstance(data, dict) else None


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def validate_viz_args(args: list[str]) -> list[str]:
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in _FORBIDDEN_VIZ_FLAGS or (flag.startswith("-o") and not flag.startswith("--")):
            raise ValueError(f"viz argument {arg!r} is not allowed (the daemon sets --output)")
    return list(args)


@dataclass
class Dataset:
    id: str
    path: str
    """The file commands run on (a fetched copy in the daemon's cache after a fetch)."""
    sha256: str
    """Fingerprint of the data the current schematic was rendered from."""
    size: int
    viz_args: list[str] = field(default_factory=list)
    schematic_path: str | None = None
    schematic_uuid: str | None = None
    title: str | None = None
    pid: str | None = None
    source_url: str | None = None
    allow_replace: bool = False
    original_path: str | None = None
    """The user's file, when ``path`` points at a fetched copy instead."""

    def public(self) -> dict[str, Any]:
        """What a paired browser may see: no paths."""
        return {
            "id": self.id,
            "title": self.title,
            "pid": self.pid,
            "can_fetch": bool(self.source_url or self.pid),
            "can_render": self.schematic_path is not None or bool(self.viz_args),
            "sha256": self.sha256,
            "bytes": self.size,
        }


class Registry:
    """Datasets persisted as JSON. Only the daemon process writes it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.datasets: dict[str, Dataset] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("datasets", []):
                ds = Dataset(**item)
                self.datasets[ds.id] = ds

    def save(self) -> None:
        data = {"version": 1, "datasets": [asdict(d) for d in self.datasets.values()]}
        write_private_file(self.path, json.dumps(data, indent=2).encode())

    def add(
        self,
        path: Path,
        *,
        fingerprint: Fingerprint,
        viz_args: list[str],
        schematic_path: Path | None = None,
        source: SchematicSource | None = None,
        title: str | None = None,
        pid: str | None = None,
        source_url: str | None = None,
        allow_replace: bool = False,
    ) -> Dataset:
        """Register ``path``, or update its entry; settings not given keep their old values."""
        resolved = str(path.resolve())
        old = next(
            (d for d in self.datasets.values() if resolved in (d.path, d.original_path)), None
        )
        schematic = str(schematic_path.resolve()) if schematic_path else None
        if old is not None and schematic is None:
            schematic = old.schematic_path
        if source is not None:
            schematic_uuid = source.schematic_uuid
        else:
            same = old is not None and old.schematic_path == schematic
            schematic_uuid = old.schematic_uuid if same and old is not None else None
        ds = Dataset(
            id=old.id if old else uuid.uuid4().hex,
            path=resolved,
            sha256=fingerprint.sha256,
            size=fingerprint.size,
            viz_args=validate_viz_args(viz_args or (old.viz_args if old else [])),
            schematic_path=schematic,
            schematic_uuid=schematic_uuid,
            title=title or (old.title if old else None) or path.name,
            pid=pid or (source.pid if source else None) or (old.pid if old else None),
            source_url=source_url or (old.source_url if old else None),
            allow_replace=allow_replace or (old.allow_replace if old else False),
        )
        self.datasets[ds.id] = ds
        self.save()
        return ds

    def remove(self, dataset_id: str) -> bool:
        if self.datasets.pop(dataset_id, None) is None:
            return False
        self.save()
        return True

    def match(self, schematic_uuid: str | None, sha256: str | None) -> Dataset | None:
        """The dataset a schematic belongs to: by its render uuid, else by data fingerprint."""
        if schematic_uuid:
            for ds in self.datasets.values():
                if ds.schematic_uuid == schematic_uuid:
                    return ds
        if sha256:
            for ds in self.datasets.values():
                if ds.sha256 == sha256.lower():
                    return ds
        return None
