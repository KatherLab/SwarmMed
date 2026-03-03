SHELL := /bin/bash
PATH := $(HOME)/.local/bin:$(PATH)
export PATH

UV := $(HOME)/.local/bin/uv
UV_INSTALL_SCRIPT := https://astral.sh/uv/install.sh
PYTHON_VERSION ?= 
REQUIREMENTS := requirements.txt
MKDOCS_CONFIG := docs/mkdocs.yml
SECRETS_DIRS := .secrets/certs .secrets/docker .secrets/pgbouncer

.DEFAULT_GOAL := help

.PHONY: help check-uv install-python install deinstall deinstall-docker env-setup setup-pgbouncer generate-certs setup start stop restart docs-install docs-serve docs-build venv compose-build compose-up compose-down compose-down-v logs sandbox-build sandbox-up sandbox-down restart-celery migrate shell test superuser tailscale

MAIN_TARGETS := start stop restart logs  migrate shell test superuser docs-serve docs-build venv deinstall

help: ## Show available targets
	@echo "✨ Main targets:"
	@for target in $(MAIN_TARGETS); do \
		desc=$$(awk -F'## ' -v target="$$target" '$$0 ~ "^" target ":" {print $$2; exit}' $(MAKEFILE_LIST)); \
		printf "  %-20s %s\n" "$$target" "$${desc:-no description}"; \
	done

check-uv: ## Ensure uv is installed before any uv commands run
	@echo "🔍 Ensuring uv is present before we run anything"
	@if ! command -v $(UV) >/dev/null 2>&1; then \
		echo "📦 Installing uv..."; \
		curl -LsSf $(UV_INSTALL_SCRIPT) | sh; \
	fi

install-python: check-uv ## Install a Python interpreter via uv
	@echo "🐍 Installing Python $(PYTHON_VERSION)"
	@$(UV) python install $(PYTHON_VERSION)
	@$(UV) python pin $(PYTHON_VERSION)

install: install-python ## Install project dependencies with uv
	@echo "🧰 Syncing project dependencies via uv pip"
	@if [ ! -d ".venv" ]; then \
		$(UV) venv; \
	fi
	@$(UV) pip sync $(REQUIREMENTS)

deinstall: deinstall-docker ## Remove uv-managed environment, caches, and generated artifacts
	@echo "🧹 Tearing down uv environment, caches, and generated artifacts"
	@rm -rf .venv .cache/uv staticfiles _build tmp workspaces || true
	@rm -rf .secrets/certs .secrets/docker .secrets/pgbouncer 2>/dev/null || true
	@if [ -d ".secrets/certs" ] || [ -d ".secrets/docker" ] || [ -d ".secrets/pgbouncer" ]; then \
		echo "🔐 Retrying secret cleanup via Docker (root-owned files detected)"; \
		docker run --rm -v "$$(pwd)/.secrets:/secrets" alpine:3 sh -c 'rm -rf /secrets/certs /secrets/docker /secrets/pgbouncer' || true; \
		rm -rf .secrets/certs .secrets/docker .secrets/pgbouncer || true; \
	fi

deinstall-docker: ## Stop SwarmCloud compose stack, drops volumes, and prunes build caches
	@echo "🧽 Stopping SwarmCloud containers and removing volumes/build caches"
	@docker compose down --remove-orphans --rmi local -v || true
	@docker builder prune -af || true
	@docker image prune -af || true

env-setup: ## Create required secret folders and copy .env template if missing
	@echo "🏗️ Ensuring secret directories and env template"
	@mkdir -p $(SECRETS_DIRS)
	@if [ -f .env ]; then \
		echo ".env already exists"; \
	else \
		cp .env.template .env; \
		echo "Created .env from template"; \
	fi

setup-pgbouncer: install ## Run the PgBouncer helper script
	@echo "🧬 Launching PgBouncer helper for secrets"
	@echo "Running scripts/setup_pgbouncer.py (optional)"
	@$(UV) run python scripts/setup_pgbouncer.py || true

generate-certs: ## Generate TLS materials inside .secrets
	@echo "🔐 Generating TLS certificates inside .secrets"
	@./scripts/generate_internal_certs.sh

setup: install env-setup setup-pgbouncer generate-certs ## Bootstrap secrets, scripts, and certificates
	@echo "🧰 Bootstrapping secrets, scripts, and certs"
	@echo "Environment setup complete"

start: setup compose-build compose-up ## Prepare the env, build assets, and start the services
	@echo "⚙️ Kicking off SwarmCloud services"
	@echo "SwarmCloud services are running"

stop: ## Stop the Docker services
	@echo "🛑 Tearing down SwarmCloud services"
	@$(MAKE) compose-down
	@echo "SwarmCloud services stopped"

restart: stop start ## Recreate the services
	@echo "♻️ Restart sequence initiated"

docs-install: install ## Install MkDocs dependencies with uv
	@echo "📚 Installing MkDocs dependencies"
	@$(UV) pip install mkdocs mkdocs-material

docs-serve: docs-install ## Serve the MkDocs documentation
	@echo "🚀 Spinning up the MkDocs dev server"
	@$(UV) run mkdocs serve --dev-addr localhost:9999

docs-build: docs-install ## Build the MkDocs documentation
	@echo "📦 Building the MkDocs site"
	@$(UV) run mkdocs build -f $(MKDOCS_CONFIG) -d _build

venv: ## Start a shell inside the uv-managed venv
	@if [ ! -d ".venv" ]; then \
		echo "No .venv detected – run make install first"; \
		exit 1; \
	fi
	@echo "👀 Launching shell inside .venv";
	@exec $(SHELL) -c 'source .venv/bin/activate && exec $(SHELL) -i'

compose-build: ## Build the Docker services
	@echo "🧱 Building the Docker services"
	@docker compose build

compose-up: ## Start the Docker services
	@echo "🎯 Bringing up the Docker services"
	@docker compose up -d

compose-down: ## Stop the Docker services
	@echo "🛑 Bringing down the Docker services"
	@docker compose down --remove-orphans

logs: ## Follow the SwarmCloud application logs
	@echo "📜 Streaming swarmcloud logs"
	@docker compose logs -f swarmcloud

compose-down-v: ## Stop services and remove the attached volumes
	@echo "🧼 Removing services and attached volumes"
	@docker compose down -v

restart-celery: ## Restart the Celery worker service
	@echo "⚡ Restarting Celery worker"
	@docker compose restart celery_worker

migrate: ## Run Django migrations inside the app container
	@echo "🧱 Applying Django migrations"
	@docker compose run --rm app python manage.py migrate

shell: ## Open a Django shell inside the app container
	@echo "🐚 Opening a Django shell session"
	@docker compose run --rm app python manage.py shell

test: ## Run Django tests inside the app container
	@echo "🧪 Running Django test suite"
	@docker compose run --rm app python manage.py test

superuser: ## Create a Django superuser inside the app container
	@echo "👑 Starting Django superuser flow"
	@docker compose run --rm app python manage.py createsuperuser

sandbox-build: ## Build only the sandbox-dind service
	@echo "🧪 Building the sandbox workload service"
	@docker compose build sandbox-dind

sandbox-up: ## Start only the sandbox-dind service
	@echo "⚡ Starting the sandbox-dind helper"
	@docker compose up -d sandbox-dind

sandbox-down: ## Stop the sandbox-dind service
	@echo "🛡️ Shutting down sandbox-dind"
	@docker compose down sandbox-dind
