# qsv-client

[![PyPI](https://img.shields.io/pypi/v/qsv-client)](https://pypi.org/project/qsv-client/)
[![codecov](https://codecov.io/gh/dathere/qsv-client/graph/badge.svg)](https://codecov.io/gh/dathere/qsv-client)

Run the [qsv](https://github.com/dathere/qsv) CSV toolkit from Python, without the usual
subprocess pitfalls:

- **Timeouts that actually stop the work.** On a timeout, task cancellation or Ctrl-C, the
  client stops qsv *and every process qsv started*: a polite stop first, then a forced kill
  after a grace period. Without that, a child qsv started would keep the output pipe open and
  hang the caller. See [Platform notes](#platform-notes) for how this works on each OS.
- **Typed errors instead of stderr scraping.** A failure raises a `QsvError` subclass
  (`QsvUsageError`, `QsvCsvError`, `QsvIOError`, `QsvNoMatch`, `QsvTimeout`, ...) that carries
  the kind, message, exit code and command, read from qsv's structured JSON error output
  (`QSV_ERROR_FORMAT=json`).
- **Capability detection.** `Qsv().capabilities` tells you the binary (`qsv`, `qsvdp`,
  `qsvlite`, `qsvmcp`), its version, compiled-in features and installed commands, from
  `qsv --capabilities`.
- **Secrets stay off the command line.** LLM settings for `describegpt` are passed as
  environment variables, so an API key never shows up in `ps` output.
- **Sync and asyncio** clients with the same API. No runtime dependencies. Python 3.11+ on
  Linux, macOS and Windows.
- **A local daemon, `qsv-clientd`,** that runs qsv for your own processes and lets a Data
  Schematic from `qsv viz smart` work with its live data, with an optional system tray to
  run and control it. See [The qsv-clientd daemon](#the-qsv-clientd-daemon).

qsv itself is not bundled, and **qsv 25.0.0 or later** is required. Install it from the
[qsv releases](https://github.com/dathere/qsv/releases) or a
[package manager](https://github.com/dathere/qsv#installation-options).

## Install

```bash
pip install qsv-client
```

## Usage

```python
from qsv_client import Qsv, QsvCsvError, QsvTimeout

qsv = Qsv(timeout=600)  # finds $QSV_BIN, else qsv/qsvmcp/qsvdp/qsvlite on PATH

qsv.count("data.csv")  # -> 1000
qsv.headers("data.csv")  # -> ["id", "name", ...]
stats = qsv.stats("data.csv", "--everything")  # one dict per column
freq = qsv.frequency("data.csv", "--limit", "20")

# anything else: run(command, *args)
res = qsv.run("sqlp", "data.csv", "select count(*) from data")
res.stdout, res.stderr, res.exit_code, res.duration
res.csv_rows()  # or res.json()

# large output: stream stdout to a file instead of holding it in memory
qsv.run("stats", "big.csv", "--everything", stdout_path="big.stats.csv")

try:
    qsv.run("select", "a", "ragged.csv", timeout=30)
except QsvCsvError as e:
    print(e.kind, e.exit_code, e.message)
except QsvTimeout:
    ...
```

`check=False` returns the result even when qsv fails. Exit codes 0 and 255 (qsv's "warning",
e.g. a broken pipe) count as success. `stdin=` feeds data on stdin; otherwise stdin is closed.
`text=False` keeps stdout as bytes in `res.stdout_bytes`, for binary output such as
`snappy compress`.

### Client options

```python
qsv = Qsv(
    "/opt/qsv/qsvlite",  # binary: path or name; default $QSV_BIN, then qsv/qsvmcp/qsvdp/qsvlite
    timeout=600,  # default per-run timeout in seconds; run(..., timeout=) overrides it
    kill_grace=5.0,  # seconds between the polite stop and the forced kill
    env={"QSV_MAX_JOBS": "4"},  # extra environment for every run; run(..., env=) adds more
    inherit_env=True,  # False starts from an empty environment (keeping SYSTEMROOT on Windows)
    cwd="/data",  # working directory for every run
)
```

A relative `binary` path is resolved against the current directory when the client is
created, so it still works with `cwd=`. A relative `stdout_path` is resolved against `cwd=`,
like a relative `--output` path given to qsv.

### describegpt without exposing your key

```python
import os

qsv = Qsv(
    llm_api_key=os.environ["OPENROUTER_API_KEY"],  # -> QSV_LLM_APIKEY
    llm_base_url="https://openrouter.ai/api/v1",  # -> QSV_LLM_BASE_URL
    llm_model="google/gemini-2.5-flash-lite",  # -> QSV_LLM_MODEL
)
dictionary = qsv.describegpt("data.csv", "--all")  # adds --format json, returns parsed JSON
```

### asyncio

```python
from qsv_client import AsyncQsv

qsv = AsyncQsv(timeout=600)
n = await qsv.count("data.csv")
caps = await qsv.capabilities()
```

Cancelling the awaiting task stops qsv and everything it started. This holds even when the
cancellation comes from `asyncio.run()` shutting down. The teardown finishes before the
cancellation propagates, so expect up to a few `kill_grace` periods.

### Capabilities and version checks

```python
qsv = Qsv(min_version="25.1.0")  # QsvVersionError on first use if older
caps = qsv.capabilities
caps.binary, caps.version, caps.features, caps.commands
caps.has_feature("polars"), caps.has_command("describegpt")
```

Capabilities are probed once per binary per process and shared by all clients, so creating a
client per call costs nothing extra after the first.

### The qsv-clientd daemon

`qsv-clientd` (installed with the package) is a per-user daemon that runs qsv for local
processes, through `DaemonClient` / `AsyncDaemonClient` (the same `run()` as `Qsv`), and for
open **Data Schematics** from `qsv viz smart` (qsv 27.0.0 or later). Once you approve a
schematic with a pairing code, it can drill into the rows behind a chart, refresh its
statistics, re-render itself, run read-only queries and fetch the latest data. Large results
are paged from the daemon, so every row is reachable.

```bash
qsv-clientd install                       # or: qsv-clientd serve
qsv-clientd register data.csv --schematic data.html
qsv-clientd approve K7Q-4MZ               # the code the schematic shows
```

```python
from qsv_client.daemon import DaemonClient

print(DaemonClient().run("count", "data.csv").stdout)
```

For a system tray icon that runs and controls the daemon, install the `tray` extra:

```bash
pipx install 'qsv-client[tray]'
qsv-clientd-tray
qsv-clientd-tray --enable-autostart       # start it at login (or tick Start at login)
```

On Linux the tray needs a desktop with AppIndicator or GTK tray support (see the doc).

See [docs/DAEMON.md](docs/DAEMON.md) for the security model and protocol.

## Errors

| Exception | qsv error kind | Typical exit code |
|---|---|---|
| `QsvUsageError` | `usage` | 2 |
| `QsvCsvError` | `csv` | 1 |
| `QsvIOError` | `io` | 1 |
| `QsvNoMatch` | `no_match` (e.g. `search` found nothing) | 1 |
| `QsvNetworkError` | `network` | 3 |
| `QsvOutOfMemory` | `out_of_memory` | 4 |
| `QsvEncodingError` | `encoding` | 5 |
| `QsvInferenceError` | `inference` (an LLM call failed) | 1 |
| `QsvWriteError` | `write` (e.g. disk full) | 1 |
| `QsvTimeout` | `timeout` (killed by this client) | 124 |
| `QsvVersionError` | `version` (binary older than 25.0.0, or than `min_version`) | |
| `QsvError` | anything else; base class of all of the above | |

`QsvDaemonError` (the daemon refused a request; `status` and `code` say why) and its subclass
`QsvDaemonUnavailable` (no daemon is listening) come only from `DaemonClient`. They are not
`QsvError`s: qsv itself did not fail.

`QsvError.structured` is True when the error came from qsv's JSON error line. A run that
dies without writing one (a panic, a signal) is classified by exit code instead: only the
exit-code categories (usage, network, out of memory, encoding) can be told apart, other
failures are `QsvError` with kind `unknown`, and the message is taken from stderr.

`QsvVersionError` is raised before the command runs; its `exit_code`, `args_run` and
`stderr` describe the `--capabilities` check. If `--capabilities` succeeds but its output
isn't recognized, `capabilities` (and the first `run`) raise a plain `QsvError`.
`QsvNotFound` (no binary) is not a `QsvError` but a `FileNotFoundError`: it is raised when
the client is created, or by a run if the binary has been removed since.

## Platform notes

On a timeout, cancellation or Ctrl-C, the client stops qsv and every process qsv started:

- **Linux, macOS:** each run gets its own session. The client sends SIGTERM to the process
  group, then SIGKILL after `kill_grace`. A process that leaves the group (by calling `setsid`)
  survives. The timeout still fires on schedule, and any output that process still holds is
  dropped.
- **Windows:** qsv is started suspended, put in its own Job Object, and only then resumed, so
  every process it starts is in the job and can't leave it. The client sends
  `CTRL_BREAK_EVENT` to the run's process group, then calls `TerminateJobObject` after
  `kill_grace`.

## Security

qsv runs as a separate process, not inside Python. That gives you a few protections:

- **Crashes and memory bugs stay in qsv.** qsv parses untrusted files. If a parser bug
  crashes it or corrupts memory, that happens in qsv's own address space, not in your
  interpreter's. Your process gets an exit code and a `QsvError`.
- **Runaway work can be stopped.** A timeout kills qsv and every process it started (see
  [Platform notes](#platform-notes)), so a hostile input that causes a hang doesn't tie up
  your process.
- **No shell.** Commands run as an argument list, never through a shell, so a filename like
  `"x.csv; rm -rf ~"` is just a filename.
- **You choose what qsv sees.** `inherit_env=False` starts qsv from an empty environment, so
  it doesn't inherit unrelated secrets (cloud credentials, tokens) from your process. Pass
  what it needs with `env=`.
- **Secrets stay off the command line.** `llm_api_key` and the other LLM settings go in
  environment variables, which `ps` doesn't show.

A subprocess is not a sandbox. Keep these limits in mind:

- qsv runs as your user and can read, write and reach the network wherever you can. To
  confine it, run your process (or qsv) under a separate user, a container, or an OS sandbox.
- `inherit_env` defaults to `True`: qsv gets your whole environment unless you turn it off.
- Environment variables are hidden from `ps` but not from your user or root, who can read a
  process's environment (for example `/proc/<pid>/environ` on Linux). Processes qsv starts
  inherit them too.
- Arguments are passed as-is. A value that comes from untrusted input and starts with `-`
  is read as a qsv option, not a file name. Validate such values, or make paths start with
  `./` or `/`.
- The client runs whatever binary `binary=`, `$QSV_BIN` or `PATH` points to. The
  `--capabilities` check confirms it behaves like a supported qsv, not that it's genuine.
  Where `PATH` or `$QSV_BIN` could be tampered with, pass an absolute path to a binary you
  installed and verified.

### Running qsv in a sandbox

The client runs `binary` with an argument list and talks to it over stdin, stdout and
stderr. So you can sandbox qsv by pointing `binary=` at a wrapper script that starts qsv
inside a sandbox. Timeouts, the process-tree kill, `stdin=`, JSON errors and the
`--capabilities` check all work through it.

```python
qsv = Qsv("/usr/local/bin/qsv-sandboxed", inherit_env=False, cwd="/srv/data/in")
```

First, use the smallest binary that has the commands you need. `qsvlite` leaves out the
commands that reach the network or run code (`fetch`, `fetchpost`, `luau`, `describegpt`,
...), and `qsv.capabilities` lists what a binary has.

On Linux, [bubblewrap](https://github.com/containers/bubblewrap) works well. Adjust the paths
for your system:

```sh
#!/bin/sh
# /usr/local/bin/qsv-sandboxed
exec bwrap \
  --die-with-parent \
  --unshare-all \
  --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/bin /bin \
  --ro-bind /opt/qsv/qsv /opt/qsv/qsv \
  --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind /srv/data/in /srv/data/in \
  --bind /srv/data/out /srv/data/out \
  --chdir "$PWD" \
  /opt/qsv/qsv "$@"
```

- `--unshare-all` turns off network access. Add `--share-net` for commands that need it,
  such as `fetch` or `describegpt`.
- qsv sees only the paths you bind. Bind them at the same path as on the host so the paths
  you pass still work.
- The working directory must be one of the bound paths, or bwrap fails at `--chdir` and
  every run (including the `--capabilities` check) errors out. Without `cwd=`, that is
  whatever directory your Python process is running in, so set `cwd=` as above.
- qsv needs to write to `/tmp` (for `extsort` and other temp files), to the output
  directory, and to the input directory if `stats` should save its cache next to the input.
- Keep `--die-with-parent`: when a timeout force-kills bwrap, the kernel then kills qsv too.
  Don't add `--new-session`. It moves qsv out of the process group the client kills, and qsv
  has no terminal that would need it.

On macOS, `sandbox-exec` with a profile works the same way: it replaces itself with qsv, so
qsv stays in the process group the client kills. Other tools, such as `firejail`, can work
too. First check that killing the wrapper with SIGKILL also stops qsv.

**Don't use `docker run` or `systemd-run` as the wrapper.** Both ask a separate manager
process to start qsv, so qsv is not in the wrapper's process group. Killing the wrapper
doesn't stop the container or service, and qsv keeps running after a timeout. Run your whole application in the
container instead, with qsv in the same image. There the client's process-group kill works
as usual, and you can add `--network none`, `--read-only`, `--cap-drop ALL`, a non-root user
and resource limits. On Windows, use a container or VM in the same way.

For any wrapper:

- Make the wrapper and the qsv binary owned by root and read-only. The `--capabilities`
  check only confirms that the wrapper behaves like qsv.
- Capabilities are cached per wrapper file, not per qsv binary. If you upgrade qsv while a
  long-running process is using it, restart the process.

## Development

```bash
uv sync --extra tray                 # the tray extra (pystray, Pillow) for its tests and mypy
uv run pytest                        # integration tests need qsv: QSV_BIN=/path/to/qsv
uv run ruff format . && uv run ruff check .
uv run mypy && uv run mypy --platform win32   # also type-checks the Windows-only code
uv run pytest --cov                  # branch coverage report
```

CI merges coverage from its Linux, macOS and Windows jobs on
[Codecov](https://codecov.io/gh/dathere/qsv-client). A local run can't reach the merged figure,
since each OS only runs its own platform-specific code.

## License

[AGPL-3.0-or-later](LICENSE)
