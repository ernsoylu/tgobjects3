---
inclusion: always
---

# Project Structure

```
gateway/app/        # the gateway service
  main.py           # FastAPI app assembly / entrypoint
  config.py         # Settings (pydantic-settings) — single source of runtime config
  format/           # on-wire/on-disk codecs: record_codec, manifests, checkpoint,
                    #   commit_record, tombstone, key_wrap, sigv4_parser, msgpack codec, types
  s3/               # S3-compatible HTTP API surface (SigV4 auth)
  admin/            # admin HTTP API
  storage/          # object/blob logic: blobs, generations, heads, staging
  telegram/         # Telegram boundary: port (Protocol), adapter (Telethon), session, errors
  cache/            # on-disk record cache
  jobs/             # background workers (GC, checkpointing, etc.)
  db/               # SQLAlchemy models, async session, Alembic migrations/

cli/                # rebuild/recovery CLI + benchmark harness
tests/              # unit/, integration/, workflow_matrix/, soak/
console/            # React + TypeScript + Vite admin SPA (stub)
deploy/             # docker-compose, Dockerfile, nginx (stub)

alembic.ini         # Alembic config (migrations under gateway/app/db/migrations)
pyproject.toml      # build, deps, ruff, mypy, pytest config
Makefile            # install / lint / typecheck / test targets
.kiro/specs/        # spec-driven requirements, design, and tasks
```

## Architecture conventions

- **Ports & adapters at the Telegram boundary.** All Telegram access goes through the `TelegramPort` Protocol in `gateway/app/telegram/port.py`. Only `telegram/adapter.py` may import Telethon; everything else depends on the port so the transport can be faked in tests or swapped out.
- **Durability lives in Telegram, not the DB.** `gateway/app/db/models.py` mirrors the design schema exactly and holds no business logic. The index must stay rebuildable from channel contents by the recovery CLI. Business logic lives in `storage/`, `format/`, and `jobs/`.
- **Typed value objects at boundaries.** Boundary types are frozen `@dataclass` / Pydantic models (e.g. `TelegramMessage`, `SessionState`, `Settings`). Prefer immutable, explicitly-typed data over dicts.
- **Typed errors, not leaked exceptions.** Adapters raise domain errors (e.g. `TelegramUnavailableError`, `FloodWaitError`) defined in the layer's `errors.py`; never let library exceptions leak to callers.
- **Config is centralized.** Consume a constructed `Settings` instance; never read `os.environ` ad hoc outside `config.py`.
- **Encryption boundary.** Never log, persist, or transmit unwrapped keys or plaintext payloads outside the encryption boundary.

## Naming & layout rules

- One concern per module; group related modules in a package with an `__init__.py`.
- Place new tests under the matching `tests/` subdirectory (`unit`, `integration`, `workflow_matrix`, `soak`).
- When adding a new package under `gateway/app/`, also add it to `[tool.setuptools].packages` in `pyproject.toml`.
- Any change to on-wire/on-disk record, checkpoint, or tombstone formats (in `format/`) must stay rebuildable by the recovery CLI, and must preserve S3 API semantics and SigV4 compatibility.

## Spec-driven workflow

This project uses Kiro specs under `.kiro/specs/tgobjects3/` (requirements, design, tasks). Source modules reference requirement numbers in docstrings — when implementing a task, keep those references accurate and consult the design document for the authoritative schema and contracts.
