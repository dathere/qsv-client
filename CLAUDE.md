# CLAUDE.md

Python library that runs the [qsv](https://github.com/dathere/qsv) CSV toolkit as a subprocess
(qsv itself is not bundled). Sync `Qsv` and asyncio `AsyncQsv` clients with the same API.
No runtime dependencies (the daemon's config is read with the stdlib's `tomllib`). Python 3.11+.

## Commands

```bash
uv sync --extra tray                     # setup (the tray extra: for its tests and mypy)
uv run pytest                            # all tests; integration tests need $QSV_BIN or qsv on PATH
QSV_BIN=/path/to/qsvlite uv run pytest -q tests/test_integration.py   # other binary variants
uv run ruff format . && uv run ruff check . && uv run mypy            # must be clean before done
uv run pytest --cov                      # with branch coverage (config in pyproject.toml)
```

CI runs lint, format check, and mypy, plus tests on ubuntu/macos x Python 3.11–3.14 against
qsv, qsvlite, and qsvdp (Linux only).
On Windows (Python 3.11–3.14) it runs the full suite against qsv, plus the integration tests
against qsvlite. Windows teardown uses Job Objects via ctypes (`_ProcessTree` in `_process.py`);
type-check it with `uv run mypy --platform win32` (the lint job runs both).
Coverage from every test job is merged on Codecov (`codecov.yml`): the total may not drop
below the base commit's, and new or changed lines need 100% coverage.

## Layout (`src/qsv_client/`)

- `client.py`: `_Base` (env, argv, min-version check, `_finish` turns the raw result into
  `QsvResult` or an exception), `Qsv`, `AsyncQsv`, `find_qsv`.
- `_process.py`: the only place that spawns and kills processes. Returns `RawResult` and never
  raises qsv errors.
- `errors.py`: the `QsvError` hierarchy and exit-code constants. `error_from_run` maps errors
  by JSON `kind` first, then by exit code.
- `capabilities.py`: parses `qsv --capabilities`.
- `daemon/`: `qsv-clientd`. `server.py` (routes, local vs browser tier), `_http.py` (minimal
  HTTP/1.1), `allowlist.py` (what paired schematics may run), `pairing.py`, `registry.py`,
  `fetch.py`, `client.py` (`DaemonClient`/`AsyncDaemonClient`, mirrored by hand), `host.py`
  (`DaemonHost`: the daemon on a background thread), `logs.py`, `__main__.py` (CLI).
  Protocol and security model: `docs/DAEMON.md`. Browsers never supply paths or viz args;
  anything a browser receives must not contain local paths.
- `tray/`: `qsv-clientd-tray`, the optional `[tray]` extra (pystray, Pillow). `model.py`
  (state to menu and icon state, pure), `controller.py` (hosts or attaches to the daemon; all
  actions on one worker thread; renders and fetches on their own daemon threads), `app.py`
  (pystray), `icons.py` (Pillow), `notify.py` (notification backends), `_open.py` (open a
  file in its app), `autostart.py` (start at login per OS), `_console.py` (Windows: attaches
  the console-less tray to a console without a window, so qsv runs open none and
  `CTRL_BREAK_EVENT` still reaches them), `_lock.py`, `__main__.py`. pystray is imported
  only in `app.py`, inside functions, and Pillow only in `icons.py`; `tests/test_tray_isolation.py` enforces that the rest imports without them.

## Invariants

- qsv 25.0.0 is the minimum (`MIN_QSV_VERSION`): the first `--capabilities` qsv with JSON
  errors. `--capabilities` is probed before the first run; a binary that rejects the flag
  raises `QsvVersionError`. `min_version=` can raise the floor, not lower it.
- Successful probes are cached process-wide in `client._caps_cache`, keyed by
  (path, size, mtime_ns) and shared by `Qsv` and `AsyncQsv`. A per-key lock (per event loop
  for async, held weakly so loops can be collected) dedups concurrent probes; sync and async
  locks are separate. The probe and the lock wait are bounded by the run's timeout (at most
  60s), separately from the command: `timeout` limits each process, not the whole `run()`. Only qsv's "Unknown flag: '--capabilities'" means `QsvVersionError`; other probe
  failures go through `error_from_run`. Tests clear the cache in an autouse fixture.
- `_Base._build_env` always sets `QSV_ERROR_FORMAT=json`. Runs that die without the JSON line
  (panics, signals) are still classified by exit code, so keep that fallback.
- Exit codes 0 and 255 (qsv's "warning") count as success.
- On timeout, cancellation, or KeyboardInterrupt, the run's whole process tree is killed after
  `kill_grace`: SIGTERM then SIGKILL to the process group on POSIX, `CTRL_BREAK_EVENT` then
  `TerminateJobObject` on Windows. A timeout raises `QsvTimeout` (exit code 124) even when
  `check=False`.
- LLM settings for describegpt are passed as env vars (`QSV_LLM_*`), never on argv.
- `min_version` is enforced inside `_probe`, so it holds however the capabilities cache is
  filled. Internal probes use `_run_unchecked` to avoid recursing into the version check.

## Conventions

- `Qsv` and `AsyncQsv` are mirrored by hand. Apply every change to both. In `AsyncQsv`,
  `capabilities` and `version` are coroutines, not properties.
- Keep it dependency-free and 3.11-compatible
  (`from __future__ import annotations`).
- mypy is strict and covers `tests/` too. Ruff line length is 100.
- New error kind: add a subclass in `errors.py`, map it in `_KIND_TO_CLASS` (and
  `_EXIT_CODE_TO_CLASS` if it has its own exit code), export it from `__init__.py`, and update
  the README errors table.
- Tests: unit tests use the `fake_qsv(body)` fixture (an executable Python stand-in for qsv).
  Integration tests use the `qsv`/`csv_files` fixtures, skip without a real binary, and must
  pass on qsv, qsvlite, and qsvdp.
