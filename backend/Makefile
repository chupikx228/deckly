.PHONY: install check guard fix test test-integration coverage-gate test-live audit run worker transcription-worker model migrate revision up down observability

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

transcription-worker:
	uv run deckly-transcription-worker

MODEL_DIRECTORY ?= models/faster-whisper-small
MODEL_REVISION = 536b0662742c02347bc0e980a01041f333bce120
MODEL_URL = https://huggingface.co/Systran/faster-whisper-small/resolve/$(MODEL_REVISION)
MODEL_CHECKSUMS = $(CURDIR)/src/deckly/infrastructure/transcription/model.sha256

model:
	@mkdir -p $(MODEL_DIRECTORY)
	@while read -r digest file; do \
		if ! (cd $(MODEL_DIRECTORY) && echo "$$digest  $$file" | shasum -a 256 --check --status 2>/dev/null); then \
			echo "downloading $$file"; \
			curl --fail --silent --show-error --location --output $(MODEL_DIRECTORY)/$$file $(MODEL_URL)/$$file || exit 1; \
		fi; \
	done < $(MODEL_CHECKSUMS)
	@cd $(MODEL_DIRECTORY) && shasum -a 256 --check --quiet $(MODEL_CHECKSUMS)

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
