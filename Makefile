.PHONY: install fix precommit format format-check lint lint-fix imports imports-check typecheck dead-code unused-deps security audit test coverage build quality ci docker-build docker-run run clean e2e-up e2e e2e-down

install:
	uv sync
	uv run prek install

precommit: fix

fix: format imports lint-fix

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check --fix .

imports:
	uv run ruff check --select I --fix .

imports-check:
	uv run ruff check --select I .

typecheck:
	uv run mypy

dead-code:
	uv run vulture src/workflows tests

unused-deps:
	uv run deptry .

security:
	uv run bandit -c pyproject.toml -r src/workflows

audit:
	uv run --with pip pip-audit

test:
	uv run pytest

coverage:
	uv run pytest --cov --cov-report=term-missing

build:
	uv build

quality: format-check lint typecheck imports-check dead-code unused-deps security audit coverage build
	@echo "quality gate passed"

ci: quality

run:
	uv run --env-file .env uvicorn --factory workflows.app:create_app --reload

docker-build:
	docker build -t workflows:local .

docker-run:
	docker run --rm -p 8000:8000 --env-file .env -v $(PWD)/data:/data workflows:local

clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache .coverage .coverage.* htmlcov dist build *.egg-info .vulture

e2e-up:
	docker compose up -d --build --wait

e2e:
	uv run pytest e2e -m e2e

e2e-down:
	docker compose down -v
