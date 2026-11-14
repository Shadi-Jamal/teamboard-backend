.DEFAULT_GOAL := help
.PHONY: help install format lint typecheck test check up down smoke

PYTHON ?= python3.12
VENV   ?= .venv
BIN    := $(VENV)/bin

help: ## Show this help
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Create the virtualenv and install pinned dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install --constraint constraints.txt --editable ".[dev]"

format: ## Format the code
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

lint: ## Check formatting and lint rules
	$(BIN)/ruff format --check .
	$(BIN)/ruff check .

typecheck: ## Run mypy in strict mode
	$(BIN)/mypy

test: ## Run the test suite with coverage (needs a running Docker daemon)
	$(BIN)/python -m pytest --cov --cov-report=term-missing

check: lint typecheck test ## Everything CI runs

up: ## Start the full stack (API on http://localhost:8000/docs)
	docker compose up --build --detach --wait

down: ## Stop the stack and delete its data
	docker compose down --volumes

smoke: ## End-to-end smoke test against the running stack
	python3 scripts/smoke_test.py http://localhost:8000
