# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The sdist now contains only the package, tests, README, CHANGELOG and LICENSE. 0.1.0's also
  included repository tooling files (`CLAUDE.md`, `.serena/`, `uv.lock`, `.github/`). The wheel
  is unchanged. ([#2](https://github.com/dathere/qsv-client/pull/2))

### Fixed

- A timeout now raises within a few `kill_grace` periods even when a process qsv started has
  left the process group and still holds its output pipes, including when qsv ignores SIGTERM.
  Previously the run waited for that process to exit. Output it still holds is dropped, and
  `AsyncQsv` closes its pipe file descriptors instead of keeping them open until it exits.
- A failed run whose stdout is not valid UTF-8 raises the typed `QsvError` instead of
  `UnicodeDecodeError`.
- `count()` and `headers()` work with `stdout_path=` and `text=False` instead of raising
  `IndexError`.
- A relative `binary` path now works together with `cwd=`. `find_qsv` returns absolute paths.

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

[Unreleased]: https://github.com/dathere/qsv-client/compare/0.1.0...HEAD
[0.1.0]: https://github.com/dathere/qsv-client/releases/tag/0.1.0
