.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
RUN := $(UV) run
COMPOSE ?= docker compose

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------- setup
.PHONY: install
install: ## Install all dependencies (including dev)
	$(UV) sync --all-extras --dev
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example — set SECRET_KEY.")

.PHONY: precommit
precommit: ## Install and run pre-commit on all files
	$(RUN) pre-commit install
	$(RUN) pre-commit run --all-files

# ---------------------------------------------------------------- code quality
.PHONY: format
format: ## Format code and fix lint issues
	$(RUN) ruff format app tests
	$(RUN) ruff check --fix app tests

.PHONY: lint
lint: ## Lint without modifying files
	$(RUN) ruff check app tests
	$(RUN) ruff format --check app tests

.PHONY: typecheck
typecheck: ## Run mypy
	$(RUN) mypy app

.PHONY: check
check: lint typecheck ## Lint and typecheck

# -------------------------------------------------------------------- database
.PHONY: migrate
migrate: ## Apply migrations up to head
	$(RUN) alembic upgrade head

.PHONY: downgrade
downgrade: ## Roll back one migration
	$(RUN) alembic downgrade -1

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="add x"
	@test -n "$(m)" || (echo 'usage: make revision m="describe the change"'; exit 1)
	$(RUN) alembic revision --autogenerate -m "$(m)"

.PHONY: seed
seed: ## Seed demo company, org structure, employees, policies and users
	$(RUN) python -m app.scripts.seed

.PHONY: create-admin
create-admin: ## Create the first admin: make create-admin email=a@b.com
	@test -n "$(email)" || (echo 'usage: make create-admin email=admin@example.com'; exit 1)
	$(RUN) python -m app.scripts.create_admin --email "$(email)"

.PHONY: rebuild-leave-balances
rebuild-leave-balances: ## Rebuild leave_balances from leave_transactions (all companies)
	$(RUN) python -m app.scripts.rebuild_leave_balances --all --verify

# ----------------------------------------------------------------------- run
.PHONY: run
run: ## Run the API with auto-reload
	$(RUN) uvicorn app.main:app --reload --host 0.0.0.0 --port 8001

.PHONY: shell
shell: ## Open a Python shell with the app importable
	$(RUN) python

# --------------------------------------------------------------------- tests
.PHONY: test
test: ## Run the full test suite with coverage
	$(RUN) pytest --cov=app --cov-report=term-missing

.PHONY: test-unit
test-unit: ## Run unit tests only (no database required)
	$(RUN) pytest tests/unit -m unit

.PHONY: test-integration
test-integration: ## Run integration tests (needs Docker for Testcontainers)
	$(RUN) pytest tests/integration -m integration

.PHONY: test-api
test-api: ## Run API tests
	$(RUN) pytest tests/api -m api

# -------------------------------------------------------------------- docker
.PHONY: docker-build
docker-build: ## Build the production image
	$(COMPOSE) build

.PHONY: docker-up
docker-up: ## Start the full stack
	$(COMPOSE) up -d
	@echo "API:   http://localhost:8001/docs"
	@echo "Redoc: http://localhost:8001/redoc"

.PHONY: docker-down
docker-down: ## Stop the stack
	$(COMPOSE) down

.PHONY: docker-clean
docker-clean: ## Stop the stack and delete volumes (destroys data)
	$(COMPOSE) down -v

.PHONY: docker-logs
docker-logs: ## Tail API logs
	$(COMPOSE) logs -f api
