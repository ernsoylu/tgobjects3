---
inclusion: always
---

# Tech Stack

## Language & runtime

- **Python 3.12+** (required). Start every module with `from __future__ import annotations`.
- Fully typed codebase: **mypy runs in strict mode** and must stay clean. Add precise type annotations to all new code; do not introduce `Any` or untyped escapes to silence errors.
- Async-first: write application code with `async`/`await` throughout. Never block the event loop with synchronous I/O.

## Build system

- **setuptools** backend (`pyproject.toml`, `build-backend = setuptools.build_meta`). The package list is maintained explicitly under `[tool.setuptools].packages` — when you add a new package under `gateway/app/`, register it there.
- Dependency versions are pinned deliberately. Keep them pinned exactly as in `pyproject.toml`; do not loosen pins or add open-ended ranges.

## Key libraries & frameworks

- **FastAPI** + **uvicorn[standard]** + **uvloop** — HTTP serving.
- **Pydantic v2** + **pydantic-settings** — models and configuration (use v2 APIs).
- **SQLAlchemy 2 (asyncio)** + **asyncpg** + **Alembic** — PostgreSQL index and migrations (use the 2.x style; the DB is a rebuildable index, not the source of truth).
- **Telethon** (+ **cryptg**) — Telegram MTProto client. Import Telethon **only** inside the adapter behind `gateway/app/telegram/port.py`; everything else depends on the port.
- **cryptography** — key wrapping and record encryption. Keep unwrapped keys and plaintext inside the encryption boundary.
- **awscrt** — AWS SigV4 helpers for the S3 API.
- **msgpack** — on-wire/on-disk record and manifest codec.

## Testing

- **pytest** with `asyncio_mode = "auto"` (async tests need no marker) and `--strict-markers`.
- **hypothesis** for property-based tests.
- **testcontainers[postgres]** for integration tests that need a real PostgreSQL.
- Place tests under the matching `tests/` subdirectory: `unit`, `integration`, or `workflow_matrix`.
- Do not add tests unless the task calls for them, but when you add or fix behavior, cover it with a test in the right subdirectory.

## Linting & style

- **ruff** (line-length 100, target py312), rules `E, F, I, W, UP, B, C4, SIM`. Let ruff's isort (`I`) order imports; do not hand-sort.
- Write thorough module and class docstrings. Existing modules document design intent and reference requirement numbers — match that depth and keep requirement references accurate.

## Common commands

```bash
make install           # pip install -e ".[dev,test]"
make lint              # ruff check .
make typecheck         # mypy (strict)
make test-unit         # pytest tests/unit
make test-integration  # pytest tests/integration
make test              # unit + integration
```

After any code change, run `make lint` and `make typecheck`, then the relevant tests. All must pass clean before you consider the change done.

## Configuration

- All runtime config flows from environment variables through `gateway/app/config.py` (`Settings`). Consume a constructed `Settings` instance; never read `os.environ` directly anywhere else.
- Required env vars: `TGOS3_MASTER_KEY` (min 16 chars) and `DATABASE_URL`. Duration strings (`7d`, `24h`) and size strings (`16GiB`) are normalized to seconds / bytes by `Settings`.
