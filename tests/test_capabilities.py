from __future__ import annotations

import json

import pytest

from qsv_client.capabilities import from_capabilities_json, from_version_output, parse_version

# real --version lines (memory figures trimmed only where noted)
QSV_ALL = (
    "qsv 24.0.0-jemalloc-apply;fetch;foreach;geocode;Luau 0.740;magika;prompt;to;viz;viz_static;"
    "polars-0.55.1:py-2.0.0;self_update-16-16;51.20 GiB-1.48 GiB-53.36 GiB-64.00 GiB "
    "(aarch64-apple-darwin compiled with Rust 1.99;macOS 27.0.1-Darwin 27.0.0;Apple M4 Max-16) "
    "prebuilt"
)
QSVDP = (
    "qsvdp 24.0.0-jemalloc-geocode;polars-0.55.1;self_update-8-8;1 GiB-0 B-2 GiB-4 GiB "
    "(x86_64-unknown-linux-musl compiled with Rust 1.99;Linux-6.1;Xeon-4) prebuilt"
)
QSVLITE_BARE = (
    "qsvlite 24.0.0-standard--4-4;1 GiB-0 B-2 GiB-4 GiB "
    "(x86_64-unknown-linux-gnu compiled with Rust 1.99;x-y;z-2) compiled"
)
PRERELEASE_MIMALLOC = (
    "qsv 24.1.0-rc1-mimalloc 212-to;polars-0.56.0;-16-16;1 GiB-0 B-2 GiB-4 GiB "
    "(aarch64-apple-darwin compiled with Rust 1.99) compiled"
)
LIST = (
    "Installed commands (3):\n\n    count       Count records\n"
    "    stats       Infer data types\n    sqlp        Run SQL\n\nsponsor"
)


def test_full_qsv_version() -> None:
    caps = from_version_output(QSV_ALL, LIST)
    assert caps.binary == "qsv"
    assert caps.version == "24.0.0"
    assert caps.features == (
        "apply",
        "fetch",
        "foreach",
        "geocode",
        "luau",
        "magika",
        "prompt",
        "to",
        "viz",
        "viz_static",
        "polars",
        "self_update",
    )
    assert caps.feature_versions == {"luau": "0.740", "polars": "0.55.1"}
    assert caps.commands == ("count", "stats", "sqlp")
    assert caps.target == "aarch64-apple-darwin"
    assert not caps.supports_json_errors
    assert caps.raw is None


def test_qsvdp_version() -> None:
    caps = from_version_output(QSVDP)
    assert caps.binary == "qsvdp"
    assert caps.features == ("geocode", "polars", "self_update")
    assert caps.target == "x86_64-unknown-linux-musl"
    assert caps.commands == ()


def test_featureless_qsvlite() -> None:
    caps = from_version_output(QSVLITE_BARE)
    assert caps.binary == "qsvlite"
    assert caps.features == ()


def test_prerelease_and_mimalloc() -> None:
    caps = from_version_output(PRERELEASE_MIMALLOC)
    assert caps.version == "24.1.0-rc1"
    assert caps.version_tuple == (24, 1, 0)
    assert caps.features == ("to", "polars")


def test_garbage_version_output() -> None:
    with pytest.raises(ValueError):
        from_version_output("not qsv at all")


def test_capabilities_json() -> None:
    doc = {
        "binary": "qsvdp",
        "version": "24.0.0",
        "target": "x86_64-unknown-linux-musl",
        "kind": "prebuilt",
        "features": ["geocode", "polars"],
        "feature_versions": {"polars": "0.55.1"},
        "commands": ["count", "stats"],
        "error_formats": ["text", "json"],
        "max_jobs": 4,
        "num_cpus": 4,
    }
    caps = from_capabilities_json(json.dumps(doc))
    assert caps.binary == "qsvdp"
    assert caps.supports_json_errors
    assert caps.has_feature("polars") and not caps.has_feature("luau")
    assert caps.has_command("stats")
    assert caps.raw == doc


def test_parse_version() -> None:
    assert parse_version("24.0.0") == (24, 0, 0)
    assert parse_version("3.10.2-beta") == (3, 10, 2)
    with pytest.raises(ValueError):
        parse_version("v24")
