SHELL := /bin/bash
PATH := $(HOME)/.local/bin:$(PATH)
export PATH

UV_INSTALL_SCRIPT := https://astral.sh/uv/install.sh
REQUIREMENTS := requirements.txt
MKDOCS_CONFIG := docs/mkdocs.yml

.DEFAULT_GOAL := help

.PHONY: help check-uv install docs-deps docs-serve docs-build compose-build compose-up compose-down manage-migrate manage-shell manage-test

help: ## Show available targets
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## ";} {printf "  %-20s %s\n", $$1, $$2}'

check-uv: ## Install uv if it is not already available
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "📦 Installing uv..."; \
		curl -LsSf $(UV_INSTALL_SCRIPT) | sh; \
	fi

install: check-uv ## Install project dependencies with uv
	@uv pip sync $(REQUIREMENTS)

docs-deps: check-uv ## Install MkDocs dependencies with uv
	@uv pip install mkdocs mkdocs-material

docs-serve: docs-deps ## Serve the MkDocs documentation
	@uv run mkdocs serve --dev-addr localhost:9999

docs-build: docs-deps ## Build the MkDocs documentation
	@uv run mkdocs build -f $(MKDOCS_CONFIG) -d _build

compose-build: ## Build the Docker services
	@docker compose build

compose-up: ## Start the Docker services
	@docker compose up -d

compose-down: ## Stop the Docker services
	@docker compose down --remove-orphans

manage-migrate: ## Run Django migrations inside the app container
	@docker compose run --rm app python manage.py migrate

manage-shell: ## Open a Django shell inside the app container
	@docker compose run --rm app python manage.py shell

manage-test: ## Run Django tests inside the app container
	@docker compose run --rm app python manage.py test
