"""What a qsv binary is and what it can do, from ``qsv --capabilities`` (qsv 25.0.0+)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, kw_only=True)
class Capabilities:
    """A qsv binary's identity and features.

    Attributes:
        binary: ``qsv``, ``qsvlite``, ``qsvdp`` or ``qsvmcp``.
        version: the qsv version, e.g. ``"25.0.0"``.
        features: optional features compiled in, e.g. ``["geocode", "polars", "self_update"]``.
        feature_versions: versions of features that have one, e.g. ``{"polars": "0.55.1"}``.
        commands: installed commands.
        target: the Rust target triple, when reported.
        raw: the parsed ``--capabilities`` document, including keys without a typed field
            (``error_formats``, ``kind``, ``max_jobs``, ``num_cpus``).
    """

    binary: str
    version: str
    features: tuple[str, ...] = ()
    feature_versions: dict[str, str] = field(default_factory=dict)
    commands: tuple[str, ...] = ()
    target: str | None = None
    raw: dict[str, Any]

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return parse_version(self.version)

    def has_feature(self, name: str) -> bool:
        return name in self.features

    def has_command(self, name: str) -> bool:
        return name in self.commands


def parse_version(version: str) -> tuple[int, int, int]:
    """``"24.0.0"`` or ``"24.1.0-rc1"`` -> ``(24, 0, 0)`` / ``(24, 1, 0)``."""
    m = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", version)
    if m is None:
        raise ValueError(f"not a qsv version: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def from_capabilities_json(text: str) -> Capabilities:
    doc = json.loads(text)
    if not isinstance(doc, dict) or "version" not in doc:
        raise ValueError("not a qsv --capabilities document")
    fv = doc.get("feature_versions") or {}
    return Capabilities(
        binary=str(doc.get("binary", "qsv")),
        version=str(doc["version"]),
        features=tuple(str(f) for f in doc.get("features", [])),
        feature_versions={str(k): str(v) for k, v in fv.items()},
        commands=tuple(str(c) for c in doc.get("commands", [])),
        target=doc.get("target"),
        raw=doc,
    )
