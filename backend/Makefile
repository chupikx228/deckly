.PHONY: install check fix test test-integration run worker migrate revision up down

install:
	uv sync --locked

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run lint-imports

fix:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest

test-integration:
	uv run pytest -m integration

run:
	uv run uvicorn deckly.main:create_app --factory --reload

worker:
	uv run deckly-worker

migrate:
	uv run alembic upgrade head

revision:
	uv run alembic revision --autogenerate -m "$(message)"

up:
	docker compose up -d --wait

down:
	docker compose down
