"""The ``qsv-clientd`` command line: ``main(argv)`` run in-process.

Most tests swap :class:`DaemonClient` for a recorder, so they check what each subcommand sends
and prints without a daemon. A few run against a real daemon in a thread.
"""

from __future__ import annotations

import io
import json
import logging
import runpy
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from daemon_helpers import DaemonThread

from qsv_client import QsvResult
from qsv_client.daemon import __main__ as cli
from qsv_client.daemon import server as server_mod
from qsv_client.daemon import service
from qsv_client.daemon.config import DaemonConfig
from qsv_client.daemon.server import Daemon
from qsv_client.errors import QsvDaemonError, QsvDaemonUnavailable, QsvError

Request = tuple[str, str, "dict[str, Any] | None", "float | None"]


class FakeClient:
    """Stands in for :class:`DaemonClient`: records requests, answers from ``replies``."""

    def __init__(self, replies: dict[tuple[str, str], Any]) -> None:
        self.replies = replies
        self.requests: list[Request] = []
        self.runs: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []
        self.run_result: QsvResult | BaseException | None = None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        wait: float | None = 30.0,
    ) -> Any:
        self.requests.append((method, path, body, wait))
        reply = self.replies.get((method, path))
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = self.request("GET", "/v1/status")
        return result

    def run(self, command: str, *args: str, **kwargs: Any) -> QsvResult:
        self.runs.append((command, args, kwargs))
        if isinstance(self.run_result, BaseException):
            raise self.run_result
        assert self.run_result is not None
        return self.run_result


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeClient]:
    def install(replies: dict[tuple[str, str], Any] | None = None) -> FakeClient:
        fake = FakeClient(replies or {})
        monkeypatch.setattr(cli, "DaemonClient", lambda: fake)
        return fake

    return install


class Terminal(io.StringIO):
    """A stdin that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _tty(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[str]:
    """Make stdin a terminal whose user types ``answer``; return the prompts shown."""
    prompts: list[str] = []
    monkeypatch.setattr(sys, "stdin", Terminal())

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def _no_tty(monkeypatch: pytest.MonkeyPatch, data: bytes = b"") -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))


def _out_json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


# -- argument parsing -----------------------------------------------------------------------


def test_a_subcommand_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "qsv-clientd" in capsys.readouterr().err


def test_unknown_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_viz_requires_output(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["viz", "data.csv"])
    assert exc.value.code == 2
    assert "--output" in capsys.readouterr().err


def test_argv_defaults_to_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["qsv-clientd", "install"])
    monkeypatch.setattr(service, "install", lambda: "installed")
    assert cli.main() == 0
    assert capsys.readouterr().out == "installed\n"


def test_python_dash_m_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["qsv-clientd", "uninstall"])
    monkeypatch.setattr(service, "uninstall", lambda: "removed")
    # a fresh copy, as `python -m qsv_client.daemon` would run (restored afterwards)
    monkeypatch.delitem(sys.modules, cli.__name__)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("qsv_client.daemon", run_name="__main__")
    assert exc.value.code == 0
    assert capsys.readouterr().out == "removed\n"


# -- serve / install / uninstall ------------------------------------------------------------


class FakeDaemon:
    instances: ClassVar[list[FakeDaemon]] = []

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self.served = False
        FakeDaemon.instances.append(self)

    async def serve_forever(self) -> None:
        self.served = True
        raise KeyboardInterrupt  # Ctrl-C ends the foreground daemon quietly


@pytest.fixture
def fake_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    FakeDaemon.instances.clear()
    monkeypatch.setattr(server_mod, "Daemon", FakeDaemon)
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(tmp_path / "home"))
    logging_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: logging_calls.append(kw))
    return logging_calls


def test_serve_uses_config_defaults(fake_daemon: list[dict[str, Any]]) -> None:
    assert cli.main(["serve"]) == 0
    (daemon,) = FakeDaemon.instances
    assert daemon.served
    assert daemon.config == DaemonConfig()
    assert fake_daemon[0]["level"] == logging.WARNING


def test_serve_flags_override_the_config_file(
    fake_daemon: list[dict[str, Any]], tmp_path: Path
) -> None:
    config = tmp_path / "custom.json"
    config.write_text(json.dumps({"port": 1234, "binary": "/from/file/qsv"}))
    assert cli.main(["serve", "--config", str(config), "-v"]) == 0
    assert FakeDaemon.instances[-1].config.port == 1234
    assert FakeDaemon.instances[-1].config.binary == "/from/file/qsv"

    assert cli.main(["serve", "--config", str(config), "--port", "0", "--binary", "qsvlite"]) == 0
    assert FakeDaemon.instances[-1].config.port == 0
    assert FakeDaemon.instances[-1].config.binary == "qsvlite"
    assert [c["level"] for c in fake_daemon] == [logging.INFO, logging.WARNING]


def test_serve_reports_a_bad_config(
    fake_daemon: list[dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "daemon.json"
    config.write_text(json.dumps({"prot": 1}))
    assert cli.main(["serve", "--config", str(config)]) == 1
    assert "unknown setting(s): prot" in capsys.readouterr().err
    assert FakeDaemon.instances == []


def test_install_and_uninstall_print_the_service_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(service, "install", lambda: "installed at /x")
    monkeypatch.setattr(service, "uninstall", lambda: "removed /x")
    assert cli.main(["install"]) == 0
    assert cli.main(["uninstall"]) == 0
    assert capsys.readouterr().out == "installed at /x\nremoved /x\n"


def test_install_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> str:
        raise QsvDaemonError("no systemctl")

    monkeypatch.setattr(service, "install", fail)
    assert cli.main(["install"]) == 1
    assert capsys.readouterr().err == "qsv-clientd: no systemctl\n"


# -- simple API subcommands -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "method", "path", "body"),
    [
        (["status"], "GET", "/v1/status", None),
        (["datasets"], "GET", "/v1/datasets", None),
        (["forget", "ds1"], "DELETE", "/v1/datasets/ds1", None),
        (["fetch", "ds1"], "POST", "/v1/datasets/ds1/fetch", {}),
        (["fetch", "ds1", "--force"], "POST", "/v1/datasets/ds1/fetch", {"force": True}),
        (["pending"], "GET", "/v1/pairings", None),
        (["deny", " abc123 "], "POST", "/v1/pairings/ABC123/deny", None),
        (["revoke"], "POST", "/v1/tokens/revoke", {"dataset_id": None}),
        (["revoke", "--dataset", "ds1"], "POST", "/v1/tokens/revoke", {"dataset_id": "ds1"}),
    ],
)
def test_api_subcommands_print_the_reply(
    fake_client: Callable[..., FakeClient],
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    reply = {"answer": argv[0]}
    fake = fake_client({(method, path): reply})
    assert cli.main(argv) == 0
    assert [(m, p, b) for m, p, b, _ in fake.requests] == [(method, path, body)]
    out = capsys.readouterr().out
    assert json.loads(out) == reply
    assert out == json.dumps(reply, indent=2) + "\n"


def test_fetch_waits_as_long_as_it_takes(fake_client: Callable[..., FakeClient]) -> None:
    fake = fake_client({("POST", "/v1/datasets/ds1/fetch"): {}})
    assert cli.main(["fetch", "ds1"]) == 0
    assert fake.requests[0][3] is None


def test_render_prints_a_message_not_the_reply(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_client({("POST", "/v1/datasets/ds1/render"): {"bytes": 10}})
    assert cli.main(["render", "ds1"]) == 0
    assert fake.requests == [("POST", "/v1/datasets/ds1/render", {}, None)]
    assert capsys.readouterr().out == "re-rendered\n"


@pytest.mark.parametrize(
    "exc",
    [
        QsvDaemonUnavailable("qsv-clientd is not running at /sock"),
        QsvError("qsv failed"),
        ValueError("bad reply"),
    ],
)
def test_errors_exit_1_with_a_message(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str], exc: Exception
) -> None:
    fake_client({("GET", "/v1/datasets"): exc})
    assert cli.main(["datasets"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"qsv-clientd: {exc}\n"


def test_other_exceptions_propagate(fake_client: Callable[..., FakeClient]) -> None:
    fake_client({("GET", "/v1/datasets"): RuntimeError("bug")})
    with pytest.raises(RuntimeError, match="bug"):
        cli.main(["datasets"])


# -- run --------------------------------------------------------------------------------------


def _result(exit_code: int = 0, stdout: bytes | None = b"out\n", stderr: str = "") -> QsvResult:
    return QsvResult(
        args=("qsv", "count"),
        exit_code=exit_code,
        stdout=None,
        stdout_bytes=stdout,
        stderr=stderr,
        duration=0.1,
    )


def test_run_passes_through_output_and_exit_code(
    fake_client: Callable[..., FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_tty(monkeypatch, b"must not be read")
    fake = fake_client()
    fake.run_result = _result(exit_code=2, stdout=b"partial\n", stderr="usage error\n")
    assert cli.main(["run", "count", "--no-headers", "data.csv"]) == 2
    (command, args, kwargs) = fake.runs[0]
    assert command == "count"
    assert args == ("--no-headers", "data.csv")
    assert kwargs == {"stdin": None, "check": False, "text": False}
    captured = capsys.readouterr()
    assert captured.out == "partial\n"
    assert captured.err == "usage error\n"


def test_run_with_no_stdout(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_client()
    fake.run_result = _result(stdout=None)
    assert cli.main(["run", "index", "data.csv"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "argv", [["run", "--stdin", "count"], ["run", "count", "-"]], ids=["--stdin", "dash"]
)
def test_run_reads_stdin_when_asked(
    fake_client: Callable[..., FakeClient], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    _no_tty(monkeypatch, b"a,b\n1,2\n")
    fake = fake_client()
    fake.run_result = _result()
    assert cli.main(argv) == 0
    assert fake.runs[0][2]["stdin"] == b"a,b\n1,2\n"


def test_run_without_a_stdin(
    fake_client: Callable[..., FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. pythonw, where sys.stdin is None
    monkeypatch.setattr(sys, "stdin", None)
    fake = fake_client()
    fake.run_result = _result()
    assert cli.main(["run", "--stdin", "count"]) == 0
    assert fake.runs[0][2]["stdin"] is None


def test_run_daemon_error(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_client()
    fake.run_result = QsvDaemonError("forbidden")
    assert cli.main(["run", "count"]) == 1
    assert capsys.readouterr().err == "qsv-clientd: forbidden\n"


# -- register / viz -------------------------------------------------------------------------


def _schematic(path: Path, **source: object) -> Path:
    blob = json.dumps({"schematic_uuid": "u1", **source})
    path.write_text(
        f'<html><script type="application/json" id="qsv-schematic-source">{blob}</script></html>'
    )
    return path


def test_register_sends_resolved_paths(
    fake_client: Callable[..., FakeClient],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = fake_client({("POST", "/v1/datasets"): {"id": "ds1"}})
    argv = ["register", "data.csv", "--source-url", "https://e.x/d.csv", "--pid", "doi:1"]
    argv += ["--title", "T", "--allow-replace", "--force"]
    assert cli.main(argv) == 0
    assert fake.requests == [
        (
            "POST",
            "/v1/datasets",
            {
                "path": str((tmp_path / "data.csv").resolve()),
                "viz_args": [],
                "schematic_path": None,
                "source_url": "https://e.x/d.csv",
                "pid": "doi:1",
                "title": "T",
                "allow_replace": True,
                "force": True,
            },
            None,
        )
    ]
    assert _out_json(capsys) == {"id": "ds1"}


def test_register_viz_args_after_double_dash(
    fake_client: Callable[..., FakeClient], tmp_path: Path
) -> None:
    html = _schematic(tmp_path / "s.html", viz_args=["--embedded"])
    fake = fake_client()
    argv = ["register", str(tmp_path / "d.csv"), "--schematic", str(html)]
    assert cli.main([*argv, "--", "--theme", "dark", "--title", "x"]) == 0
    body = fake.requests[0][2]
    assert body is not None
    # explicit arguments win; the embedded ones are not even offered
    assert body["viz_args"] == ["--theme", "dark", "--title", "x"]
    assert body["title"] is None
    assert body["schematic_path"] == str(html.resolve())


def test_register_accepts_embedded_viz_args_with_yes(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    html = _schematic(tmp_path / "s.html", viz_args=["--theme", "dark"])
    fake = fake_client({("POST", "/v1/datasets"): {"id": "ds1"}})
    assert cli.main(["register", str(tmp_path / "d.csv"), "--schematic", str(html), "-y"]) == 0
    body = fake.requests[0][2]
    assert body is not None
    assert body["viz_args"] == ["--theme", "dark"]
    out = capsys.readouterr().out
    assert out.startswith("The schematic was rendered with: qsv viz smart --theme dark\n")


@pytest.mark.parametrize(("answer", "accepted"), [("y", True), (" YES ", True), ("n", False)])
def test_register_asks_about_embedded_viz_args(
    fake_client: Callable[..., FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    answer: str,
    accepted: bool,
) -> None:
    prompts = _tty(monkeypatch, answer)
    html = _schematic(tmp_path / "s.html", viz_args=["--theme", "dark"])
    fake = fake_client()
    assert cli.main(["register", str(tmp_path / "d.csv"), "--schematic", str(html)]) == 0
    assert prompts == ["Use these arguments when the daemon re-renders it? [y/N] "]
    body = fake.requests[0][2]
    assert body is not None
    assert body["viz_args"] == (["--theme", "dark"] if accepted else [])


def test_register_without_a_terminal_declines_embedded_viz_args(
    fake_client: Callable[..., FakeClient], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_tty(monkeypatch)
    html = _schematic(tmp_path / "s.html", viz_args=["--theme", "dark"])
    fake = fake_client()
    assert cli.main(["register", str(tmp_path / "d.csv"), "--schematic", str(html)]) == 0
    body = fake.requests[0][2]
    assert body is not None
    assert body["viz_args"] == []


def test_register_explains_redacted_viz_args(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    html = _schematic(
        tmp_path / "s.html", viz_args=["--logo", "logo.png"], redacted=["--logo", "--css"]
    )
    fake = fake_client()
    assert cli.main(["register", str(tmp_path / "d.csv"), "--schematic", str(html), "-y"]) == 0
    body = fake.requests[0][2]
    assert body is not None
    # even with --yes: the paths are gone, so the arguments cannot be reused
    assert body["viz_args"] == []
    out = capsys.readouterr().out
    assert "file paths were removed from the schematic: --logo, --css." in out


@pytest.mark.parametrize("kind", ["no-source", "no-viz-args", "missing-file"])
def test_register_with_nothing_to_offer(
    fake_client: Callable[..., FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kind: str,
) -> None:
    prompts = _tty(monkeypatch, "y")
    html = tmp_path / "s.html"
    if kind == "no-source":
        html.write_text("<html>a plain page</html>")
    elif kind == "no-viz-args":
        _schematic(html)
    fake = fake_client({("POST", "/v1/datasets"): {"id": "ds1"}})
    assert cli.main(["register", str(tmp_path / "d.csv"), "--schematic", str(html)]) == 0
    assert prompts == []
    body = fake.requests[0][2]
    assert body is not None
    assert body["viz_args"] == []
    assert body["schematic_path"] == str(html.resolve())
    assert _out_json(capsys) == {"id": "ds1"}


def test_viz_renders_through_the_daemon(
    fake_client: Callable[..., FakeClient],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = fake_client({("POST", "/v1/datasets"): {"id": "ds1"}})
    argv = ["viz", "d.csv", "-o", "out.html", "--source-url", "https://e.x/d.csv"]
    assert cli.main([*argv, "--", "--theme", "dark"]) == 0
    assert fake.requests == [
        (
            "POST",
            "/v1/datasets",
            {
                "path": str((tmp_path / "d.csv").resolve()),
                "viz_args": ["--theme", "dark"],
                "schematic_path": str((tmp_path / "out.html").resolve()),
                "source_url": "https://e.x/d.csv",
                "render": True,
            },
            None,
        )
    ]
    assert _out_json(capsys) == {"id": "ds1"}


# -- approve ----------------------------------------------------------------------------------


def _pairing(origin: str = "null", **dataset: object) -> dict[str, Any]:
    ds: dict[str, Any] = {"title": "Sales", "path": "/data/sales.csv", **dataset}
    return {"code": "ABC123", "origin": origin, "dataset": ds}


def test_approve_unknown_code(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_client({("GET", "/v1/pairings"): [_pairing()]})
    assert cli.main(["approve", "zzz999", "-y"]) == 1
    assert "no pending pairing with code ZZZ999" in capsys.readouterr().err
    assert [r[:2] for r in fake.requests] == [("GET", "/v1/pairings")]


def test_approve_unregistered_schematic(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    pairing = _pairing()
    pairing["dataset"] = None
    fake = fake_client({("GET", "/v1/pairings"): [pairing]})
    assert cli.main(["approve", "abc123", "-y"]) == 1
    assert "matches no registered dataset" in capsys.readouterr().err
    assert len(fake.requests) == 1


def test_approve_with_yes(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_client(
        {
            ("GET", "/v1/pairings"): [_pairing()],
            ("POST", "/v1/pairings/ABC123/approve"): {"approved": True},
        }
    )
    assert cli.main(["approve", " abc123", "--yes"]) == 0
    assert [r[:2] for r in fake.requests][-1] == ("POST", "/v1/pairings/ABC123/approve")
    out = capsys.readouterr().out
    assert out.startswith(
        "Pair code ABC123 for 'Sales' (/data/sales.csv)\n"
        "Origin: null (a page opened from a file, or a sandboxed frame).\n"
    )
    assert json.loads(out.split("frame).\n", 1)[1]) == {"approved": True}


def test_approve_warns_about_fetched_copy_and_foreign_origin(
    fake_client: Callable[..., FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    pairing = _pairing(origin="https://evil.example", original_path="/data/orig.csv")
    fake_client({("GET", "/v1/pairings"): [pairing], ("POST", "/v1/pairings/ABC123/approve"): {}})
    assert cli.main(["approve", "ABC123", "-y"]) == 0
    out = capsys.readouterr().out
    assert "(the fetched copy of /data/orig.csv, which is not being read)" in out
    assert "WARNING: the request came from 'https://evil.example', not a local file." in out


def test_approve_without_a_terminal_needs_yes(
    fake_client: Callable[..., FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_tty(monkeypatch)
    fake = fake_client({("GET", "/v1/pairings"): [_pairing()]})
    assert cli.main(["approve", "ABC123"]) == 1
    assert "re-run with --yes" in capsys.readouterr().err
    assert len(fake.requests) == 1


@pytest.mark.parametrize(("answer", "approved"), [("y", True), ("", False), ("nope", False)])
def test_approve_asks_on_a_terminal(
    fake_client: Callable[..., FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    approved: bool,
) -> None:
    prompts = _tty(monkeypatch, answer)
    fake = fake_client(
        {("GET", "/v1/pairings"): [_pairing()], ("POST", "/v1/pairings/ABC123/approve"): {}}
    )
    assert cli.main(["approve", "ABC123"]) == (0 if approved else 1)
    assert prompts == ["Approve? [y/N] "]
    sent = [r[:2] for r in fake.requests]
    assert (("POST", "/v1/pairings/ABC123/approve") in sent) is approved
    if not approved:
        assert capsys.readouterr().err == "not approved\n"


# -- against a real daemon ------------------------------------------------------------------


@pytest.fixture
def live_daemon(
    monkeypatch: pytest.MonkeyPatch, fake_qsv: Callable[[str], str]
) -> Iterator[DaemonThread]:
    # short path: macOS limits Unix socket paths to 104 bytes, too short for pytest's tmp_path
    home = Path(tempfile.mkdtemp(prefix="qcm"))
    monkeypatch.setenv("QSV_CLIENTD_HOME", str(home))
    monkeypatch.delenv("QSV_CLIENTD_SOCKET", raising=False)
    binary = fake_qsv(
        """
        sys.stdout.write("argv=" + " ".join(sys.argv[1:]) + "\\n")
        # like the stdout prelude in fake_qsv: no "\\r\\n" on Windows
        sys.stderr.reconfigure(newline="\\n")
        sys.stderr.write("note\\n")
        sys.exit(3 if "fail" in sys.argv else 0)
        """
    )
    thread = DaemonThread(Daemon(DaemonConfig(binary=binary), home=home, tcp_port=0)).start()
    yield thread
    thread.stop()
    shutil.rmtree(home, ignore_errors=True)


def test_live_status_and_run(
    live_daemon: DaemonThread, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_tty(monkeypatch)
    assert cli.main(["status"]) == 0
    status = _out_json(capsys)
    assert status["port"] == live_daemon.port

    assert cli.main(["run", "count", "x.csv"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "argv=count x.csv\n"
    assert captured.err == "note\n"

    assert cli.main(["run", "count", "fail"]) == 3


def test_live_errors_from_the_daemon(
    live_daemon: DaemonThread, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["forget", "no-such-dataset"]) == 1
    assert capsys.readouterr().err.startswith("qsv-clientd: ")
    assert cli.main(["datasets"]) == 0
    assert _out_json(capsys) == []
