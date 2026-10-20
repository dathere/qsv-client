# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `qsv-clientd`, a per-user daemon that runs qsv for local processes and for Data
  Schematics. Local processes connect over a Unix socket, or on Windows over loopback with a
  per-user token. Data Schematics connect over loopback once paired with a code that you
  approve. A paired schematic can drill into rows, refresh stats, re-render itself, run
  allowlisted read-only queries and fetch the latest data. `DaemonClient` and
  `AsyncDaemonClient` take the same `run()` arguments as `Qsv` and raise the same errors.
  It uses only the standard library. See `docs/DAEMON.md`.
- `QsvError.to_dict()` and `qsv_client.errors.error_from_dict()`.
- `QsvDaemonError` and `QsvDaemonUnavailable`.

### Changed

- A `min_version` that is not a version (`"major.minor.patch"`) now raises `ValueError` when
  `Qsv` or `AsyncQsv` is created, instead of on every run.
- `QsvNotFound` is now a `FileNotFoundError`. A run whose binary was removed after the
  client was created raises it, instead of a bare `FileNotFoundError`; existing `OSError`
  handlers still catch it.

### Fixed

- `DaemonClient` raises `QsvDaemonError` for a garbled reply from `qsv-clientd`, as
  `AsyncDaemonClient` already did, instead of leaking `http.client` exceptions.
- The `schtasks` command that `qsv-clientd install` prints on Windows works when a path
  contains spaces.
- A Data Schematic query's option values must be strings or numbers, like its other
  arguments.
- `qsv-clientd run search -- -x` passes the `--` on to qsv, so a pattern or argument that
  starts with `-` works. argparse used to drop it.

## [0.4.0] - 2026-10-15

qsv 25.0.0 or later is now required. It is the first release whose binaries (`qsv`,
`qsvlite`, `qsvdp`, `qsvmcp`) all answer `--capabilities` and report errors as JSON, so the
code that supported older qsv is gone.

### Changed

- **Behavior change:** qsv 25.0.0 or later is now required (exported as `MIN_QSV_VERSION`).
  Before the first command, `qsv --capabilities` is now always run, even without
  `min_version=`. A qsv that is too old, or that rejects `--capabilities`, raises
  `QsvVersionError` instead of running. `min_version=` can still raise the floor, but not
  lower it. The check is bounded by the run's `timeout` (at most 60s) separately from the
  command, so a client's first run can take up to about twice its timeout. A check that fails
  for another reason, such as a crash or a startup error, raises the usual `QsvError` with
  qsv's stderr. ([#9](https://github.com/dathere/qsv-client/pull/9))
- **Behavior change:** `QsvVersionError` is now a `QsvError` (kind `version`), so
  `except QsvError` around `run()` also catches a too-old binary. An `except QsvVersionError`
  placed after `except QsvError` is no longer reached, so put it first. Generic `QsvError`
  handlers now see version errors too: retry logic should not retry them, since a too-old
  qsv will never succeed. Its `exit_code`, `args_run` and `stderr` describe the
  `--capabilities` check. ([#10](https://github.com/dathere/qsv-client/pull/10))
- `capabilities` is probed once per binary per Python process and shared by every `Qsv` and
  `AsyncQsv`, so creating a client per call doesn't start an extra qsv each time. Concurrent
  first calls from threads, or from coroutines on the same event loop, share one probe; a
  sync and an async client starting at the same moment can each probe once. A binary
  replaced in place (new size or modification time) is probed again. Each client still
  applies its own `min_version` and gets its own copy of the result, so modifying
  `Capabilities.raw` or `feature_versions` affects only that client. Values in `raw` that
  depend on the environment, such as `max_jobs`, come from the client that probed first.
  ([#9](https://github.com/dathere/qsv-client/pull/9), [#10](https://github.com/dathere/qsv-client/pull/10))

### Removed

- The fallback that parsed `--version` and `--list` when `--capabilities` was unavailable,
  including `qsv_client.capabilities.from_version_output`. If `--capabilities` succeeds but
  its output can't be used, `capabilities` raises `QsvError`.
  ([#9](https://github.com/dathere/qsv-client/pull/9))
- `Capabilities.supports_json_errors` and `Capabilities.error_formats`: every supported qsv
  reports JSON errors. The value is still in `raw["error_formats"]`.
  ([#10](https://github.com/dathere/qsv-client/pull/10))
- `Capabilities.raw` is no longer optional; it is always the parsed `--capabilities` document.
  `Capabilities` fields are now keyword-only. ([#10](https://github.com/dathere/qsv-client/pull/10))

## [0.3.0] - 2026-10-10

### Changed

- **Behavior change:** a relative `stdout_path` is now resolved against the client's `cwd=`,
  like a relative `--output` path given to qsv. Previously it was resolved against Python's
  working directory, so code combining `cwd=` with a relative `stdout_path` will now write its
  output file somewhere else, without any error. Pass an absolute path to keep the old location.
  ([#7](https://github.com/dathere/qsv-client/pull/7))

### Fixed

- `AsyncQsv`: `QsvTimeout.stderr` now includes what qsv wrote before the timeout, as it
  already did for `Qsv`. Previously, stderr already read when the timeout fired was lost.
  ([#6](https://github.com/dathere/qsv-client/pull/6))
- `capabilities` (and `run` with `min_version`) raise `QsvError` instead of a bare
  `ValueError` when the binary's `--version` output isn't recognized, and fall back to `--version`
  when `--capabilities` returns JSON of an unexpected shape or without a usable version.
  ([#7](https://github.com/dathere/qsv-client/pull/7), [#8](https://github.com/dathere/qsv-client/pull/8))
- The `QsvError` docstring named the argv attribute `args`; it is `args_run`. ([#7](https://github.com/dathere/qsv-client/pull/7))
- On Windows, `find_qsv` (and `binary=` / `QSV_BIN`) accept a path without its extension,
  such as `C:/tools/qsv` for `C:/tools/qsv.exe`, trying each `PATHEXT` extension.
  ([#8](https://github.com/dathere/qsv-client/pull/8))

## [0.2.0] - 2026-10-09

### Added

- Windows support. qsv is started suspended and placed in its own Job Object before it runs,
  so a timeout, cancellation or Ctrl-C stops qsv *and* everything it started:
  `CTRL_BREAK_EVENT` first, then `TerminateJobObject` after `kill_grace`. Previously only
  `qsv.exe` itself was killed. With `inherit_env=False`, `SYSTEMROOT` is still passed on
  Windows, since many programs fail to start without it. CI runs the full test suite on
  Windows. ([#4](https://github.com/dathere/qsv-client/pull/4),
  [#5](https://github.com/dathere/qsv-client/pull/5))

### Changed

- The sdist now contains only the package, tests, README, CHANGELOG and LICENSE. 0.1.0's also
  included repository tooling files (`CLAUDE.md`, `.serena/`, `uv.lock`, `.github/`). The wheel
  is unchanged. ([#2](https://github.com/dathere/qsv-client/pull/2))
- Tested on Python 3.14 as well as 3.10–3.13. ([#3](https://github.com/dathere/qsv-client/pull/3))

### Fixed

- A timeout now raises within a few `kill_grace` periods even when a process qsv started has
  left the process group and still holds its output pipes, including when qsv ignores SIGTERM.
  Previously the run waited for that process to exit. Output it still holds is dropped, and
  `AsyncQsv` closes its pipe file descriptors instead of keeping them open until it exits.
  ([#3](https://github.com/dathere/qsv-client/pull/3))
- Cancelling an `AsyncQsv` run now always kills qsv and everything it started, even when the
  cancellation arrives during timeout cleanup or comes from `asyncio.run()` shutting down. A
  cancelled run finishes its teardown before the cancellation propagates.
  ([#3](https://github.com/dathere/qsv-client/pull/3))
- A second Ctrl-C while a sync run is waiting out `kill_grace` no longer skips the forced kill,
  which left a qsv that ignores the polite stop running. A Ctrl-C right after qsv starts is
  handled the same way. ([#5](https://github.com/dathere/qsv-client/pull/5))
- On Windows with Python 3.10–3.12, a run whose child stops reading a large `stdin=` payload
  no longer hangs past its timeout. ([#5](https://github.com/dathere/qsv-client/pull/5))
- A failed run whose stdout is not valid UTF-8 raises the typed `QsvError` instead of
  `UnicodeDecodeError`. ([#3](https://github.com/dathere/qsv-client/pull/3))
- `count()` and `headers()` work with `stdout_path=` and `text=False` instead of raising
  `IndexError`. ([#3](https://github.com/dathere/qsv-client/pull/3))
- A relative `binary` path now works together with `cwd=`. `find_qsv` returns absolute paths.
  ([#3](https://github.com/dathere/qsv-client/pull/3))

## [0.1.0] - 2026-10-08

First release. ([#1](https://github.com/dathere/qsv-client/pull/1))

### Added

- `Qsv` (sync) and `AsyncQsv` (asyncio) clients with the same API: `run(command, *args)` plus
  `count`, `headers`, `stats`, `frequency` and `describegpt` conveniences, returning a
  `QsvResult` with `json()` and `csv_rows()` helpers.
- Binary discovery: an explicit path, then `$QSV_BIN`, then `qsv`, `qsvmcp`, `qsvdp` or
  `qsvlite` on `PATH`.
- Timeouts and cancellation that stop the whole process group (SIGTERM, then SIGKILL after a
  grace period), including children qsv started itself. On Windows, qsv is killed directly.
- Typed errors: `QsvError` and its subclasses `QsvUsageError`, `QsvCsvError`, `QsvIOError`,
  `QsvNoMatch`, `QsvNetworkError`, `QsvOutOfMemory`, `QsvEncodingError`, `QsvInferenceError`,
  `QsvWriteError` and `QsvTimeout`. They are read from qsv's JSON error line
  (`QSV_ERROR_FORMAT=json`, [dathere/qsv#4765](https://github.com/dathere/qsv/pull/4765)) when the
  binary supports it, and classified by exit code otherwise. `QsvNotFound` and `QsvVersionError`
  are raised before anything runs.
- Capability detection (`Qsv().capabilities`): binary, version, features and commands, read from
  `qsv --capabilities` where available, otherwise from `--version` and `--list`. A `min_version=`
  check is also available.
- `stdout_path=` streams large output to a file instead of holding it in memory.
- `llm_api_key`, `llm_base_url` and `llm_model` are passed to `describegpt` as `QSV_LLM_*`
  environment variables, so keys never appear on the command line.
- Type hints (`py.typed`), no runtime dependencies, Python 3.10+.

[Unreleased]: https://github.com/dathere/qsv-client/compare/0.4.0...HEAD
[0.4.0]: https://github.com/dathere/qsv-client/compare/0.3.0...0.4.0
[0.3.0]: https://github.com/dathere/qsv-client/compare/0.2.0...0.3.0
[0.2.0]: https://github.com/dathere/qsv-client/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/dathere/qsv-client/releases/tag/0.1.0
