# CLAUDE.md

Python library that runs the [qsv](https://github.com/dathere/qsv) CSV toolkit as a subprocess
(qsv itself is not bundled). Sync `Qsv` and asyncio `AsyncQsv` clients with the same API.
No runtime dependencies. Python 3.10+.

## Commands

```bash
uv sync                                  # setup
uv run pytest                            # all tests; integration tests need $QSV_BIN or qsv on PATH
QSV_BIN=/path/to/qsvlite uv run pytest -q tests/test_integration.py   # other binary variants
uv run ruff format . && uv run ruff check . && uv run mypy            # must be clean before done
```

CI runs lint, format check, and mypy, plus tests on ubuntu/macos x Python 3.10–3.14 against
qsv, qsvlite, and qsvdp (Linux only).

## Layout (`src/qsv_client/`)

- `client.py`: `_Base` (env, argv, min-version check, `_finish` turns the raw result into
  `QsvResult` or an exception), `Qsv`, `AsyncQsv`, `find_qsv`.
- `_process.py`: the only place that spawns and kills processes. Returns `RawResult` and never
  raises qsv errors.
- `errors.py`: the `QsvError` hierarchy and exit-code constants. `error_from_run` maps errors
  by JSON `kind` first, then by exit code.
- `capabilities.py`: parses `qsv --capabilities`, falling back to `--version` + `--list`.

## Invariants

- `_Base._build_env` always sets `QSV_ERROR_FORMAT=json`. Older qsv versions ignore it, so
  their errors are classified by exit code.
- Exit codes 0 and 255 (qsv's "warning") count as success.
- On timeout, cancellation, or KeyboardInterrupt, the whole process group is killed: SIGTERM,
  then SIGKILL after `kill_grace`. A timeout raises `QsvTimeout` (exit code 124) even when
  `check=False`.
- LLM settings for describegpt are passed as env vars (`QSV_LLM_*`), never on argv.
- `min_version` is enforced inside `_probe`, so it holds however the capabilities cache is
  filled. Internal probes use `_run_unchecked` to avoid recursing into the version check.

## Conventions

- `Qsv` and `AsyncQsv` are mirrored by hand. Apply every change to both. In `AsyncQsv`,
  `capabilities` and `version` are coroutines, not properties.
- Keep it dependency-free and 3.10-compatible (`from __future__ import annotations`).
- mypy is strict and covers `tests/` too. Ruff line length is 100.
- New error kind: add a subclass in `errors.py`, map it in `_KIND_TO_CLASS` (and
  `_EXIT_CODE_TO_CLASS` if it has its own exit code), export it from `__init__.py`, and update
  the README errors table.
- Tests: unit tests use the `fake_qsv(body)` fixture (an executable Python stand-in for qsv).
  Integration tests use the `qsv`/`csv_files` fixtures, skip without a real binary, and must
  pass on qsv, qsvlite, and qsvdp.
