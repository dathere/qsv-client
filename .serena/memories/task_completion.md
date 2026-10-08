# Task completion

Run (mirrors CI lint + test jobs), all must pass:
1. `uv run ruff format .`
2. `uv run ruff check .`
3. `uv run mypy`
4. `uv run pytest` (with a real qsv available so integration tests don't skip)

If the public API or error table changed, update README.md accordingly. If sync client changed, confirm the async mirror changed too (see `mem:conventions`).
