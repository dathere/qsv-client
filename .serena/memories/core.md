# qsv-client — core

Python library that runs the qsv CSV toolkit binary as a subprocess (qsv not bundled). Zero runtime deps. Sync `Qsv` + asyncio `AsyncQsv` with identical API.

Related: stack/versions `mem:tech_stack`; dev/test/run commands `mem:suggested_commands`; code style & sync/async mirroring rules `mem:conventions`; done-checklist (ruff/mypy/pytest as CI runs them) `mem:task_completion`.

## Source map (`src/qsv_client/`)
- `client.py` — `_Base` (init, env building, argv, min-version check, `_finish` → result/exception), `Qsv`, `AsyncQsv`, `QsvResult`, `find_qsv` (`$QSV_BIN`, else first of qsv/qsvmcp/qsvdp/qsvlite on PATH).
- `_process.py` — private spawn/kill layer: `run_sync`/`run_async` return `RawResult`; never raises qsv errors.
- `errors.py` — `QsvError` hierarchy, exit-code constants, `parse_json_error`, `error_from_run` (kind→class via `_KIND_TO_CLASS`, fallback exit code→class via `_EXIT_CODE_TO_CLASS`).
- `capabilities.py` — `Capabilities` dataclass; parsers `from_capabilities_json` (`qsv --capabilities`) and `from_version_output` (`--version` + `--list` fallback).
- `__init__.py` — public re-exports; `py.typed` shipped.

## Invariants
- Every run gets `QSV_ERROR_FORMAT=json` forced in `_Base._build_env` (overrides user env). Older qsv ignores it → classify by exit code.
- Success = exit code 0 or 255 (`SUCCESS_EXIT_CODES`; 255 = qsv "warning", e.g. broken pipe).
- Timeout/cancel/KeyboardInterrupt: kill whole process group (POSIX `start_new_session`): SIGTERM, then SIGKILL after `kill_grace`. Timeout → `QsvTimeout` with exit code 124 (`EXIT_TIMEOUT`), raised even when `check=False`.
- stdin is DEVNULL unless `stdin=` given.
- LLM secrets go via env (`QSV_LLM_APIKEY`/`_BASE_URL`/`_MODEL`), never argv.
- `min_version` is enforced inside `_probe` (so it holds however the capabilities cache is filled); `run()` calls `_check_version_once()` first; internal probes use `_run_unchecked` to avoid recursion.
- Empty `command` string → argv is just `[binary, *args]` (used for `--version`/`--list`/`--capabilities`).
- Windows: new process group, only qsv itself killed (children not reaped). Officially POSIX/macOS.
