from __future__ import annotations

import json

import pytest

from qsv_client.capabilities import from_capabilities_json, parse_version


def test_capabilities_json() -> None:
    doc = {
        "binary": "qsvdp",
        "version": "25.0.0",
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
    assert caps.has_feature("polars") and not caps.has_feature("luau")
    assert caps.has_command("stats")
    assert caps.raw == doc


def test_parse_version() -> None:
    assert parse_version("24.0.0") == (24, 0, 0)
    assert parse_version("3.10.2-beta") == (3, 10, 2)
    with pytest.raises(ValueError):
        parse_version("v24")
