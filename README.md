# tgobjects3

S3-compatible object storage gateway backed by Telegram channels.

## Layout

```
gateway/app/   # gateway application (format, s3, admin, storage, telegram, cache, jobs, db)
cli/           # rebuild / recovery CLI
tests/         # unit, integration, workflow_matrix
console/       # React + TypeScript + Vite SPA (stub)
deploy/        # docker-compose, Dockerfile, nginx (stub)
```

## Development

```
make install           # install with dev + test extras
make lint              # ruff
make typecheck         # mypy (strict)
make test-unit         # pytest tests/unit
make test-integration  # pytest tests/integration
```

Requires Python 3.12+.
