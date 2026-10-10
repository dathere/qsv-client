# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  when `--capabilities` returns JSON of an unexpected shape. ([#7](https://github.com/dathere/qsv-client/pull/7))
- The `QsvError` docstring named the argv attribute `args`; it is `args_run`. ([#7](https://github.com/dathere/qsv-client/pull/7))
- On Windows, `find_qsv` (and `binary=` / `QSV_BIN`) accept a path without its extension,
  such as `C:/tools/qsv` for `C:/tools/qsv.exe`, trying each `PATHEXT` extension.

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

[Unreleased]: https://github.com/dathere/qsv-client/compare/0.3.0...HEAD
[0.3.0]: https://github.com/dathere/qsv-client/compare/0.2.0...0.3.0
[0.2.0]: https://github.com/dathere/qsv-client/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/dathere/qsv-client/releases/tag/0.1.0
