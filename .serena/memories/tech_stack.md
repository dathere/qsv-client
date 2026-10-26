# Tech stack

- Python >=3.11 (ruff `target-version = py311`; CI matrix 3.11–3.14 on ubuntu + macos). Use `from __future__ import annotations`; no 3.12+ only stdlib features.
- No runtime dependencies — keep it that way (stdlib `subprocess`/`asyncio` only). daemon.toml is read with stdlib `tomllib`.
- Package/env manager: uv (`uv.lock` committed); dev deps in `[dependency-groups].dev`: pytest, pytest-asyncio, mypy, ruff.
- Build: hatchling, src layout (`src/qsv_client`). License AGPL-3.0-or-later.
- pytest: `asyncio_mode = "auto"` (async tests need no marker).
- mypy `strict = true` over `src` AND `tests`.
- ruff line-length 100; lint rules E,F,W,I,B,UP,SIM,RUF; `ruff format` enforced.
- Release: GitHub release published → `.github/workflows/publish.yml` builds with `uv build`, PyPI Trusted Publishing (env `pypi`). Version is static in `pyproject.toml`.
- External: real qsv binary needed only for integration tests; CI downloads latest dathere/qsv release and tests qsv, qsvlite, and (Linux) qsvdp variants.
