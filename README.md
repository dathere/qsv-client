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
  the kind, message, exit code and command. Newer qsv builds report these as structured JSON
  (`QSV_ERROR_FORMAT=json`). Older ones are classified from their exit code.
- **Capability detection.** `Qsv().capabilities` tells you the binary (`qsv`, `qsvdp`,
  `qsvlite`, `qsvmcp`), its version, compiled-in features and installed commands. It reads
  `qsv --capabilities` where available and falls back to parsing `--version` and `--list`.
- **Secrets stay off the command line.** LLM settings for `describegpt` are passed as
  environment variables, so an API key never shows up in `ps` output.
- **Sync and asyncio** clients with the same API. No runtime dependencies. Python 3.10+ on
  Linux, macOS and Windows.

qsv itself is not bundled. Install it from the
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
created, so it still works with `cwd=`.

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
qsv = Qsv(min_version="24.0.0")  # QsvVersionError on first use if older
caps = qsv.capabilities
caps.binary, caps.version, caps.features, caps.commands
caps.has_feature("polars"), caps.has_command("describegpt"), caps.supports_json_errors
```

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
| `QsvError` | anything else; base class of all of the above | |

`QsvError.structured` is True when the error came from qsv's JSON error line. With qsv
versions that predate it, only the exit-code categories (usage, network, out of memory,
encoding) can be told apart; other failures are `QsvError` with kind `unknown`, and the
message is taken from stderr.

`QsvNotFound` (no binary) and `QsvVersionError` (binary too old) are raised before anything
runs.

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

## Development

```bash
uv sync
uv run pytest                        # integration tests need qsv: QSV_BIN=/path/to/qsv
uv run ruff format . && uv run ruff check .
uv run mypy && uv run mypy --platform win32   # also type-checks the Windows-only code
```

## License

[AGPL-3.0-or-later](LICENSE)
