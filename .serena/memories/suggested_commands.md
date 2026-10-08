# Suggested commands

- Setup: `uv sync`
- All tests: `uv run pytest` (integration tests use `$QSV_BIN`, else `qsv` on PATH; skip if none)
- Against a specific binary variant: `QSV_BIN=/path/to/qsvlite uv run pytest -q tests/test_integration.py`
- Single test: `uv run pytest tests/test_process.py -k <name> -v`
- Lint: `uv run ruff check .` (autofix: `--fix`)
- Format: `uv run ruff format .` (CI uses `--check`)
- Types: `uv run mypy` (files configured in pyproject)
- Build wheel/sdist: `uv build` (outputs to `dist/`, gitignored)

Darwin notes: BSD `sed -i` needs `''` arg; `grep -P` unavailable (use `rg` or `grep -E`).
