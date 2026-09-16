# ---------------------------------------------------------------------------
# Configuration (override on the command line, e.g. make docker-build-prod REGISTRY_NAME=foo)
# ---------------------------------------------------------------------------
PYTHON ?= python3
VENV := .venv
VENV_BIN := $(VENV)/bin

APP_PORT ?= 8000
IMAGE_NAME ?= myservice
REGISTRY_NAME ?= $(shell echo $$REGISTRY_NAME)
REGISTRY := registry.digitalocean.com/$(REGISTRY_NAME)/$(IMAGE_NAME)
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

.DEFAULT_GOAL := help

.PHONY: help install dev test test-docker test-coverage ci lint lint-fix \
        docker-build docker-build-prod docker-run docker-stop clean

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------
install: ## Create a virtualenv and install dependencies
	$(PYTHON) -m venv $(VENV)
	$(VENV_BIN)/pip install --upgrade pip
	$(VENV_BIN)/pip install -r requirements.txt

dev: ## Run the API locally with auto-reload
	$(VENV_BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port $(APP_PORT)

# ---------------------------------------------------------------------------
# Testing and quality
# ---------------------------------------------------------------------------
test: ## Run the test suite
	$(VENV_BIN)/python -m pytest tests/ -v

test-docker: ## Run the test suite in a Python 3.11 container
	docker run --rm -v "$(PWD)":/app -w /app python:3.11-slim \
		bash -c "pip install -q --no-cache-dir -r requirements.txt && python -m pytest tests/ -v"

test-coverage: ## Run tests with a coverage report
	$(VENV_BIN)/python -m pytest tests/ --cov=app --cov-report=term-missing

ci: lint ## Run the same checks CI runs
	$(VENV_BIN)/python -m pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=70

lint: ## Check code style
	$(VENV_BIN)/ruff check app/ tests/

lint-fix: ## Check code style and apply safe fixes
	$(VENV_BIN)/ruff check app/ tests/ --fix

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
docker-build: ## Build the image for local use
	docker build -t $(IMAGE_NAME):local .

docker-build-prod: ## Build for linux/amd64 and push to the DO registry
	@test -n "$(REGISTRY_NAME)" || (echo "REGISTRY_NAME is required" && exit 1)
	docker buildx build \
		--platform linux/amd64 \
		--tag $(REGISTRY):latest \
		--tag $(REGISTRY):$(GIT_SHA) \
		--push .

docker-run: ## Start the stack with docker compose
	docker compose up --build

docker-stop: ## Stop the stack
	docker compose down

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
clean: ## Remove caches, bytecode and test databases
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[cod]' -delete
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	rm -f tests/test_*.db
	rm -rf tests/test_storage
