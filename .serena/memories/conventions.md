# Conventions

- `Qsv` and `AsyncQsv` are hand-mirrored: same method set (`run`, `_run_unchecked`, `capabilities`, `version`, `_probe`, `_check_version_once`, conveniences). Any change to one must be applied to the other. In async, `capabilities`/`version` are coroutines, not properties.
- Shared non-I/O logic lives in `_Base`; keep I/O in subclasses and `_process.py`.
- `_process.py` is the only place that spawns/kills processes; it returns `RawResult` and leaves error classification to `_Base._finish` → `errors.error_from_run`.
- Convenience methods take `(path, *args, **kw)` and forward `**kw` to `run`; `Arg` = str | int | float | PathLike.
- New qsv error kind: add subclass in `errors.py`, map in `_KIND_TO_CLASS` (and `_EXIT_CODE_TO_CLASS` if it has a distinct exit code), export from `__init__.py`, update README errors table.
- Full type hints everywhere (mypy strict, tests included). Google-style docstrings (`Args:`/`Raises:`) on public API; private helpers mostly undocumented.
- Comments only for non-obvious WHY (lowercase, terse).
- Tests: unit tests use the `fake_qsv(body)` fixture (conftest) — writes an executable Python script standing in for qsv; integration tests use `qsv`/`qsv_bin`/`csv_files` fixtures and skip without a real binary. Integration tests must pass on qsv, qsvlite and qsvdp.
