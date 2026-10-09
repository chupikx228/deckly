.PHONY: install check guard fix test test-integration coverage-gate test-live audit run worker migrate revision up down observability

install:
	uv sync --locked

check: guard
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run lint-imports
	uv run vulture

guard:
	@if grep -rniE '#\s*(type:\s*ignore|mypy:\s*ignore-errors|noqa|ruff:\s*noqa|pragma:\s*no cover)' src tests migrations; then \
		echo 'error: suppression comments are not allowed; fix the code instead'; exit 1; fi

fix:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run coverage erase
	uv run pytest --cov --cov-report=

test-integration:
	uv run pytest -m integration --cov --cov-append --cov-report=

coverage-gate:
	uv run coverage report --fail-under=95
	uv run coverage report --format=total --include='src/deckly/domain/*,src/deckly/application/*' --fail-under=98
	uv run coverage report --format=total --include='src/deckly/infrastructure/*,src/deckly/transport/*,src/deckly/worker/*,src/deckly/main.py' --fail-under=90

audit:
	@requirements=$$(mktemp) && \
		uv export --quiet --locked --no-emit-project --no-annotate --format requirements-txt > $$requirements && \
		uvx --from pip-audit==2.10.1 pip-audit --requirement $$requirements --require-hashes --disable-pip --strict; \
		status=$$?; rm -f $$requirements; exit $$status

test-live:
	uv run pytest -m live

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

observability:
	docker compose --profile observability up -d prometheus
