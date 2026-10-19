# qsv-client

[![PyPI](https://img.shields.io/pypi/v/qsv-client)](https://pypi.org/project/qsv-client/)

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
- **Sync and asyncio** clients with the same API. No runtime dependencies. Python 3.10+ on
  Linux, macOS and Windows.

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

`QsvError.structured` is True when the error came from qsv's JSON error line. A run that
dies without writing one (a panic, a signal) is classified by exit code instead: only the
exit-code categories (usage, network, out of memory, encoding) can be told apart, other
failures are `QsvError` with kind `unknown`, and the message is taken from stderr.

`QsvVersionError` is raised before the command runs; its `exit_code`, `args_run` and
`stderr` describe the `--capabilities` check. If `--capabilities` succeeds but its output
isn't recognized, `capabilities` (and the first `run`) raise a plain `QsvError`.
`QsvNotFound` (no binary) is not a `QsvError`: it is raised when the client is created.

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

## Development

```bash
uv sync
uv run pytest                        # integration tests need qsv: QSV_BIN=/path/to/qsv
uv run ruff format . && uv run ruff check .
uv run mypy && uv run mypy --platform win32   # also type-checks the Windows-only code
```

## License

[AGPL-3.0-or-later](LICENSE)
