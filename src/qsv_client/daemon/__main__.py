"""The ``qsv-clientd`` command."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

from ..errors import QsvDaemonError, QsvError, QsvNotFound
from . import logs, service
from ._text import NO_DATASET, describe_pairing
from .client import DaemonClient
from .config import DaemonConfig
from .registry import SchematicSource

_CKAN_API_HELP = (
    "the CKAN site (https://data.example.gov) or its Action API URL, for ckan:// sources"
)
_URL_SCHEMES = ("http", "https", "ckan", "dathere", "s3", "gs", "az")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qsv-clientd", description="Run qsv for local processes and Data Schematics."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the daemon in the foreground")
    s.add_argument("--config", type=Path, help="config file (default: daemon.json)")
    s.add_argument("--port", type=int, help="loopback port for browsers (0 disables)")
    s.add_argument("--binary", help="qsv binary to use (overrides daemon.json)")
    s.add_argument("--log-file", type=Path, help="also log to this file (rotated at 5 MB, 3 kept)")
    s.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("status", help="show what the running daemon is doing")
    sub.add_parser("install", help="install and start as a user service")
    sub.add_parser("uninstall", help="stop and remove the user service")

    r = sub.add_parser("run", help="run a qsv command through the daemon")
    r.add_argument(
        "--stdin",
        action="store_true",
        help="send this process's stdin to the command (implied when an argument is '-')",
    )
    # one REMAINDER for the command and its args: argparse drops a `--` that starts a REMAINDER,
    # and `run search -- -x` must reach qsv with its `--`
    r.add_argument("argv", nargs=argparse.REMAINDER, metavar="command [args ...]")

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
    reg.add_argument("--ckan-api", help=_CKAN_API_HELP)

    v = sub.add_parser(
        "viz",
        help="render a schematic through the daemon and register it",
        description="Render a Data Schematic through the daemon and register it. DATA may be a "
        "URL instead of a file: an http(s) download link, ckan://<id>, or a CKAN resource or "
        "dataset page. The data is then downloaded first, and the URL is where Fetch latest "
        "gets new data from.",
    )
    v.add_argument("data", help="a data file, or a URL to download it from")
    v.add_argument(
        "-o", "--output", type=Path, help="the schematic to write (default: DATA's name, .html)"
    )
    v.add_argument("--source-url", help="where to fetch the latest data from (for a file DATA)")
    v.add_argument("--ckan-api", help=_CKAN_API_HELP)
    v.add_argument(
        "--save-as",
        type=Path,
        help="where to save downloaded data (default: beside the schematic, named after the URL)",
    )
    v.add_argument(
        "--no-open", action="store_true", help="don't open the schematic in your browser"
    )

    sub.add_parser("datasets", help="list registered datasets")
    f = sub.add_parser("forget", help="unregister a dataset and revoke its tokens")
    f.add_argument("dataset_id")
    for name, help_ in (
        ("render", "re-render a dataset's schematic"),
        ("fetch", "fetch a dataset's latest data"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("dataset_id")
        if name == "fetch":
            sp.add_argument(
                "--force",
                action="store_true",
                help="use the new data even if some of the current columns are gone",
            )

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
    # on stderr: input() would write it to stdout, into a pipe reading this command's JSON
    print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
    return input().strip().lower() in ("y", "yes")


def _serve(ns: argparse.Namespace) -> int:
    from .server import Daemon

    logs.configure_logging(logging.INFO if ns.verbose else logging.WARNING, ns.log_file)
    config = DaemonConfig.load(ns.config)
    if ns.port is not None:
        config.port = ns.port
    if ns.binary is not None:
        config.binary = ns.binary
    daemon = Daemon(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.serve_forever())
    return 0


def _register(client: DaemonClient, ns: argparse.Namespace) -> int:
    viz_args: list[str] = ns.viz_args
    if ns.schematic is not None and ns.schematic.is_file() and not viz_args:
        source = SchematicSource.from_html(ns.schematic.read_text("utf-8", "replace"))
        if source is not None and source.viz_args:
            # stdout is the registration's JSON alone, so it can be piped
            print(
                "The schematic was rendered with: qsv viz smart " + " ".join(source.viz_args),
                file=sys.stderr,
            )
            if source.redacted:
                print(
                    "These flags' file paths were removed from the schematic: "
                    + ", ".join(source.redacted)
                    + ". To re-render, register again with the full arguments after --.",
                    file=sys.stderr,
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
        "ckan_api": ns.ckan_api,
    }
    _print(client.request("POST", "/v1/datasets", body, wait=None))
    return 0


def _is_url(value: str) -> bool:
    scheme, sep, _ = value.partition("://")
    return bool(sep) and scheme.lower() in _URL_SCHEMES


def _data_name(url: str) -> str:
    """A file name for data downloaded from ``url``: its last path segment, as CSV if unsure."""
    parts = urllib.parse.urlsplit(url)
    segments = [s for s in parts.path.split("/") if s] or [parts.netloc]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", urllib.parse.unquote(segments[-1])).strip("._")
    name = name or "data"
    if not name.lower().endswith((".csv", ".tsv", ".tab", ".ssv")):
        name += ".csv"
    return name


def _viz(client: DaemonClient, ns: argparse.Namespace) -> int:
    body: dict[str, Any] = {"viz_args": ns.viz_args, "ckan_api": ns.ckan_api, "render": True}
    if _is_url(ns.data):
        if ns.source_url is not None:
            print(
                "qsv-clientd: --source-url is for a file; the URL given is the source",
                file=sys.stderr,
            )
            return 2
        folder = ns.output.parent if ns.output is not None else Path.cwd()
        data = (ns.save_as or folder / _data_name(ns.data)).resolve()
        body.update(path=str(data), source_url=ns.data, download=True)
    else:
        data = Path(ns.data).resolve()
        body.update(path=str(data), source_url=ns.source_url)
    output = (ns.output or data.with_suffix(".html")).resolve()
    body["schematic_path"] = str(output)
    _print(client.request("POST", "/v1/datasets", body, wait=None))
    if body.get("download"):
        print(f"Saved the data to {data}", file=sys.stderr)
    print(f"Wrote the Data Schematic to {output}", file=sys.stderr)
    opened = not ns.no_open and sys.stdin.isatty() and webbrowser.open(output.as_uri())
    if not opened:
        print(f"Open it in your browser: {output.as_uri()}", file=sys.stderr)
    print(
        "When the page shows a code, approve it with: qsv-clientd approve <CODE>", file=sys.stderr
    )
    return 0


def _approve(client: DaemonClient, ns: argparse.Namespace) -> int:
    wanted = ns.code.strip().upper()
    pending = [p for p in client.request("GET", "/v1/pairings") if p["code"] == wanted]
    if not pending:
        print(
            f"no pending pairing with code {wanted} (codes expire after 5 minutes)", file=sys.stderr
        )
        return 1
    text = describe_pairing(pending[0])
    if not text.can_approve:
        print(NO_DATASET, file=sys.stderr)
        return 1
    print(text.headline)
    if text.fetched_note:
        print(f"  {text.fetched_note}")
    print(text.origin_line)
    if not ns.yes and not sys.stdin.isatty():
        print("no terminal to confirm on; re-run with --yes to approve", file=sys.stderr)
        return 1
    if not (ns.yes or _confirm("Approve?")):
        print("not approved", file=sys.stderr)
        return 1
    _print(client.request("POST", f"/v1/pairings/{text.code}/approve"))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `register`/`viz` take qsv viz arguments after a literal `--`
    viz_args: list[str] = []
    if argv[:1] in (["register"], ["viz"]) and "--" in argv:
        i = argv.index("--")
        argv, viz_args = argv[:i], argv[i + 1 :]
    parser = _parser()
    ns = parser.parse_args(argv)
    ns.viz_args = viz_args
    if ns.cmd == "run":
        if ns.argv[:1] == ["--"]:  # `run -- search ...`: ends qsv-clientd's own options
            del ns.argv[0]
        if not ns.argv:
            parser.error("run: the following arguments are required: command")
        ns.command, ns.args = ns.argv[0], ns.argv[1:]
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
            # only when asked: the whole of stdin is read up front, which would swallow a shell
            # loop's input or hang on a pipe that never closes
            wants = ns.stdin or "-" in ns.args
            stdin = sys.stdin.buffer.read() if wants and sys.stdin is not None else None
            result = client.run(ns.command, *ns.args, stdin=stdin, check=False, text=False)
            sys.stdout.buffer.write(result.stdout_bytes or b"")
            sys.stderr.write(result.stderr)
            return result.exit_code
        elif ns.cmd == "register":
            return _register(client, ns)
        elif ns.cmd == "viz":
            return _viz(client, ns)
        elif ns.cmd == "datasets":
            _print(client.request("GET", "/v1/datasets"))
        elif ns.cmd == "forget":
            _print(client.request("DELETE", f"/v1/datasets/{ns.dataset_id}"))
        elif ns.cmd == "render":
            client.request("POST", f"/v1/datasets/{ns.dataset_id}/render", {}, wait=None)
            print("re-rendered")
        elif ns.cmd == "fetch":
            body = {"force": True} if ns.force else {}
            _print(client.request("POST", f"/v1/datasets/{ns.dataset_id}/fetch", body, wait=None))
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
