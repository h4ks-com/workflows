PYTHON := uv run

.PHONY: install fix precommit format format-check lint lint-fix imports imports-check typecheck dead-code unused-deps security audit test coverage build quality ci docker-build docker-run run clean

install:
	uv sync
	uv run pre-commit install

precommit: fix

fix: format imports lint-fix

format:
	$(PYTHON) ruff format .

format-check:
	$(PYTHON) ruff format --check .

lint:
	$(PYTHON) ruff check .

lint-fix:
	$(PYTHON) ruff check --fix .

imports:
	$(PYTHON) ruff check --select I --fix .

imports-check:
	$(PYTHON) ruff check --select I .

typecheck:
	$(PYTHON) mypy

dead-code:
	$(PYTHON) vulture src/workflows tests

unused-deps:
	$(PYTHON) deptry .

security:
	$(PYTHON) bandit -c pyproject.toml -r src/workflows

audit:
	uv run --with pip pip-audit

test:
	$(PYTHON) pytest

coverage:
	$(PYTHON) pytest --cov --cov-report=term-missing

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
