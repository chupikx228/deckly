# Deckly generation service

FastAPI service that turns a topic into reviewable notes. The contract, architecture and testing
rules live in [`../.claude/backend/`](../.claude/backend/) — read `handoff.md` first.

## Layout

```
src/deckly/
  main.py            composition root: builds the FastAPI app, wires adapters, registers handlers
  config.py          settings from the environment (pydantic-settings), fail-fast on anything missing
  transport/         HTTP: routers, Problem (RFC 9457) model, the single error mapper
  application/       use cases and the ports they depend on; application errors
  domain/            pure business rules; domain errors
  infrastructure/    adapters: async SQLAlchemy + asyncpg, Arq/Redis, JSON logging
  worker/            Arq worker entrypoint and WorkerSettings
migrations/          Alembic (async), baseline revision only
```

Dependencies point inward only. `make check` runs import-linter, which fails the build if
`domain` or `application` import an outer layer or an I/O/SDK package (FastAPI, SQLAlchemy,
Redis, Arq…), or if `transport` reaches infrastructure directly.

## Errors

Domain and application code raise typed errors (`domain/exceptions.py`,
`application/exceptions.py`). `transport/error_handlers.py` is the only place that turns them
into `application/problem+json`, using the `code` table from `api-contract.md`. Request
validation failures become `400 VALIDATION_FAILED`; anything unmapped is logged with its
traceback and returned as `500 INTERNAL_ERROR` with no internal detail.

## Running locally

Requires [uv](https://docs.astral.sh/uv/) and Docker. Run everything from `backend/`.

```bash
make install        # uv sync --locked (installs Python 3.12 if needed)
cp .env.example .env
make up             # Postgres on :5433, Redis on :6380
make migrate        # alembic upgrade head
make run            # uvicorn on :8000 with reload
```

Every setting is required; the app refuses to start if one is missing, blank or invalid, and
if Postgres or Redis are unreachable. The database URL must use `postgresql+asyncpg`.

The worker (`make worker`) has no task functions yet, and Arq refuses to start a worker without
one; it becomes runnable once the generation task lands.

## Checks

```bash
make check          # ruff format --check, ruff check, mypy --strict, import-linter
make fix            # ruff format + ruff check --fix
make test           # pytest
```

CI runs `make install`, `make check` and `make test` (`.github/workflows/ci.yml`, `backend` job).
