"""The ``qsv-clientd`` command."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from ..errors import QsvDaemonError, QsvError, QsvNotFound
from . import service
from .client import DaemonClient
from .config import DaemonConfig
from .registry import SchematicSource


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qsv-clientd", description="Run qsv for local processes and Data Schematics."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the daemon in the foreground")
    s.add_argument("--config", type=Path, help="config file (default: daemon.json)")
    s.add_argument("--port", type=int, help="loopback port for browsers (0 disables)")
    s.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("status", help="show what the running daemon is doing")
    sub.add_parser("install", help="install and start as a user service")
    sub.add_parser("uninstall", help="stop and remove the user service")

    r = sub.add_parser("run", help="run a qsv command through the daemon")
    r.add_argument("command")
    r.add_argument("args", nargs=argparse.REMAINDER)

    reg = sub.add_parser("register", help="let a Data Schematic for this file use the daemon")
    reg.add_argument("data", type=Path)
    reg.add_argument("--schematic", type=Path, help="the schematic HTML rendered from DATA")
    reg.add_argument("--source-url", help="where to fetch the latest data from")
    reg.add_argument("--pid", help="persistent identifier (overrides the schematic's)")
    reg.add_argument("--title")
    reg.add_argument(
        "--allow-replace",
        action="store_true",
        help="let a fetch overwrite DATA (default: keep fetched data in the cache)",
    )
    reg.add_argument("--force", action="store_true", help="register even if fingerprints differ")
    reg.add_argument("-y", "--yes", action="store_true", help="accept embedded viz arguments")

    v = sub.add_parser("viz", help="render a schematic through the daemon and register it")
    v.add_argument("data", type=Path)
    v.add_argument("-o", "--output", type=Path, required=True)
    v.add_argument("--source-url")

    sub.add_parser("datasets", help="list registered datasets")
    f = sub.add_parser("forget", help="unregister a dataset and revoke its tokens")
    f.add_argument("dataset_id")
    for name, help_ in (
        ("render", "re-render a dataset's schematic"),
        ("fetch", "fetch a dataset's latest data"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("dataset_id")

    sub.add_parser("pending", help="list schematics waiting to be paired")
    a = sub.add_parser("approve", help="approve a schematic's pairing code")
    a.add_argument("code")
    a.add_argument("-y", "--yes", action="store_true")
    d = sub.add_parser("deny", help="deny a pairing code")
    d.add_argument("code")
    rv = sub.add_parser("revoke", help="revoke paired schematics' tokens")
    rv.add_argument("--dataset", help="only this dataset's tokens")
    return p


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def _serve(ns: argparse.Namespace) -> int:
    from .server import Daemon

    logging.basicConfig(
        level=logging.INFO if ns.verbose else logging.WARNING,
        format="%(asctime)s qsv-clientd %(levelname)s %(message)s",
    )
    config = DaemonConfig.load(ns.config)
    if ns.port is not None:
        config.port = ns.port
    daemon = Daemon(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.serve_forever())
    return 0


def _register(client: DaemonClient, ns: argparse.Namespace) -> int:
    viz_args: list[str] = ns.viz_args
    if ns.schematic is not None and ns.schematic.is_file() and not viz_args:
        source = SchematicSource.from_html(ns.schematic.read_text("utf-8", "replace"))
        if source is not None and source.viz_args:
            print("The schematic was rendered with: qsv viz smart " + " ".join(source.viz_args))
            if source.redacted:
                print(
                    "These flags' file paths were removed from the schematic: "
                    + ", ".join(source.redacted)
                    + ". To re-render, register again with the full arguments after --."
                )
            elif ns.yes or _confirm("Use these arguments when the daemon re-renders it?"):
                viz_args = list(source.viz_args)
    body = {
        "path": str(ns.data.resolve()),
        "viz_args": viz_args,
        "schematic_path": str(ns.schematic.resolve()) if ns.schematic else None,
        "source_url": ns.source_url,
        "pid": ns.pid,
        "title": ns.title,
        "allow_replace": ns.allow_replace,
        "force": ns.force,
    }
    _print(client.request("POST", "/v1/datasets", body, wait=None))
    return 0


def _approve(client: DaemonClient, ns: argparse.Namespace) -> int:
    wanted = ns.code.strip().upper()
    pending = [p for p in client.request("GET", "/v1/pairings") if p["code"] == wanted]
    if not pending:
        print(
            f"no pending pairing with code {wanted} (codes expire after 5 minutes)", file=sys.stderr
        )
        return 1
    p = pending[0]
    ds = p["dataset"]
    if ds is None:
        print(
            "This schematic matches no registered dataset. Register it first:\n"
            "  qsv-clientd register <data.csv> --schematic <schematic.html>",
            file=sys.stderr,
        )
        return 1
    print(f"Pair code {p['code']} for {ds['title']!r} ({ds['path']})")
    if p["origin"] != "null":
        print(f"WARNING: the request came from {p['origin']!r}, not a local file.")
    if not (ns.yes or _confirm("Approve?")):
        print("not approved", file=sys.stderr)
        return 1
    _print(client.request("POST", f"/v1/pairings/{p['code']}/approve"))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `register`/`viz` take qsv viz arguments after a literal `--`
    viz_args: list[str] = []
    if argv[:1] in (["register"], ["viz"]) and "--" in argv:
        i = argv.index("--")
        argv, viz_args = argv[:i], argv[i + 1 :]
    ns = _parser().parse_args(argv)
    ns.viz_args = viz_args
    try:
        if ns.cmd == "serve":
            return _serve(ns)
        if ns.cmd == "install":
            print(service.install())
            return 0
        if ns.cmd == "uninstall":
            print(service.uninstall())
            return 0
        client = DaemonClient()
        if ns.cmd == "status":
            _print(client.status())
        elif ns.cmd == "run":
            result = client.run(ns.command, *ns.args, check=False, text=False)
            sys.stdout.buffer.write(result.stdout_bytes or b"")
            sys.stderr.write(result.stderr)
            return result.exit_code
        elif ns.cmd == "register":
            return _register(client, ns)
        elif ns.cmd == "viz":
            body = {
                "path": str(ns.data.resolve()),
                "viz_args": ns.viz_args,
                "schematic_path": str(ns.output.resolve()),
                "source_url": ns.source_url,
                "render": True,
            }
            _print(client.request("POST", "/v1/datasets", body, wait=None))
        elif ns.cmd == "datasets":
            _print(client.request("GET", "/v1/datasets"))
        elif ns.cmd == "forget":
            _print(client.request("DELETE", f"/v1/datasets/{ns.dataset_id}"))
        elif ns.cmd == "render":
            client.request("POST", f"/v1/datasets/{ns.dataset_id}/render", {}, wait=None)
            print("re-rendered")
        elif ns.cmd == "fetch":
            _print(client.request("POST", f"/v1/datasets/{ns.dataset_id}/fetch", {}, wait=None))
        elif ns.cmd == "pending":
            _print(client.request("GET", "/v1/pairings"))
        elif ns.cmd == "approve":
            return _approve(client, ns)
        elif ns.cmd == "deny":
            _print(client.request("POST", f"/v1/pairings/{ns.code.strip().upper()}/deny"))
        elif ns.cmd == "revoke":
            _print(client.request("POST", "/v1/tokens/revoke", {"dataset_id": ns.dataset}))
    except (QsvDaemonError, QsvError, QsvNotFound, ValueError) as exc:
        print(f"qsv-clientd: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
