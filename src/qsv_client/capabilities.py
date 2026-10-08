"""What a qsv binary is and what it can do.

Binaries released after qsv 24.0.0 answer ``--capabilities`` with JSON. For older ones the
same information is recovered from ``--version`` (binary, version and features) and
``--list`` (commands).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# `qsvdp 24.0.0-jemalloc-geocode;polars-0.55.1:py-2.0.0;self_update-16-16;12.3 GiB-...`
# The pre-release slot is optional; the allocator is `mimalloc <ver>`, `jemalloc`,
# `jemalloc+bgthread` or `standard`.
_VERSION_RE = re.compile(
    r"^(?P<binary>\S+) (?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)-"
    r"(?P<malloc>mimalloc [^-]+|jemalloc\+bgthread|jemalloc|standard)-"
    r"(?P<features>.*?)-?(?P<maxjobs>\d+)-(?P<numcpus>\d+);"
)
_LIST_LINE_RE = re.compile(r"^ {4}(?P<cmd>[a-z][a-z0-9]*)\s")


@dataclass(frozen=True)
class Capabilities:
    """A qsv binary's identity and features.

    Attributes:
        binary: ``qsv``, ``qsvlite``, ``qsvdp`` or ``qsvmcp``.
        version: the qsv version, e.g. ``"24.0.0"``.
        features: optional features compiled in, e.g. ``["geocode", "polars", "self_update"]``.
        feature_versions: versions of features that have one, e.g. ``{"polars": "0.55.1"}``.
        commands: installed commands, or an empty tuple when they could not be determined.
        error_formats: supported ``QSV_ERROR_FORMAT`` values. ``("text",)`` means the binary
            predates structured errors.
        target: the Rust target triple, when reported.
        raw: the parsed ``--capabilities`` document, or None when derived from ``--version``.
    """

    binary: str
    version: str
    features: tuple[str, ...] = ()
    feature_versions: dict[str, str] = field(default_factory=dict)
    commands: tuple[str, ...] = ()
    error_formats: tuple[str, ...] = ("text",)
    target: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return parse_version(self.version)

    @property
    def supports_json_errors(self) -> bool:
        return "json" in self.error_formats

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
        error_formats=tuple(str(f) for f in doc.get("error_formats", ["text"])),
        target=doc.get("target"),
        raw=doc,
    )


def _normalize_feature(token: str) -> tuple[str, str | None]:
    """Map a ``--version`` feature token to (name, version)."""
    if token.startswith("Luau"):
        rest = token[len("Luau") :].strip()
        return "luau", (rest if rest and not rest.startswith("-") else None)
    if token.startswith("python-"):
        return "python", token[len("python-") :]
    if token.startswith("polars-"):
        return "polars", token[len("polars-") :].split(":", 1)[0]
    return token, None


def from_version_output(version_text: str, list_text: str = "") -> Capabilities:
    line = version_text.strip().splitlines()[0] if version_text.strip() else ""
    m = _VERSION_RE.match(line)
    if m is None:
        raise ValueError(f"unrecognized qsv --version output: {line!r}")
    features: list[str] = []
    versions: dict[str, str] = {}
    for token in m.group("features").split(";"):
        token = token.strip()
        if not token:
            continue
        name, ver = _normalize_feature(token)
        features.append(name)
        if ver:
            versions[name] = ver
    target_m = re.search(r"\((?P<target>[\w.-]+) compiled with Rust", line)
    commands = tuple(
        cm.group("cmd")
        for cm in (_LIST_LINE_RE.match(ln) for ln in list_text.splitlines())
        if cm is not None
    )
    return Capabilities(
        binary=m.group("binary"),
        version=m.group("version"),
        features=tuple(features),
        feature_versions=versions,
        commands=commands,
        target=target_m.group("target") if target_m else None,
    )
