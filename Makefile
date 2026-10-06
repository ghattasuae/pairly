# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# Python always runs through uv (Python 3.12, see .python-version). JS runs through pnpm.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV   ?= uv
PNPM ?= pnpm

# Docker Compose stack (infra/docker-compose.yml). Extra profiles: web, scan, observability.
#   PROFILES="scan observability" make dev
PROFILES     ?=
COMPOSE_FILE := infra/docker-compose.yml
COMPOSE      := docker compose --env-file .env -f $(COMPOSE_FILE)
PROFILE_ARGS := $(foreach p,$(PROFILES),--profile $(p))
# Seconds `make dev` waits for every service to report healthy.
WAIT_TIMEOUT ?= 300
# Optional service filter for `make logs`, e.g. SERVICE=api
SERVICE      ?=

.PHONY: help install hooks env dev demo down logs ps test evals lint format typecheck

help: ## List targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python (uv) and JS (pnpm) dependencies
	$(UV) sync --all-packages
	$(PNPM) install

hooks: ## Install the pre-commit git hooks
	$(UV) run pre-commit install

env: ## Create .env with random secrets if it is missing (never overwrites)
	@$(UV) run python scripts/gen_dev_env.py

dev: env ## Start the stack and wait until healthy (PROFILES="scan observability web")
	$(COMPOSE) $(PROFILE_ARGS) up -d --build --remove-orphans --wait --wait-timeout $(WAIT_TIMEOUT)
	@$(COMPOSE) $(PROFILE_ARGS) ps
	@echo ""
	@echo "Pairly is up. API http://localhost:8000  Mail UI http://localhost:8025  MinIO console http://localhost:9001"

demo: dev ## Boot everything healthy (seed data arrives in M2)
	@echo ""
	@echo "make demo: the stack is healthy. Seeded demo data arrives in M2, so there is nothing to explore yet."

down: ## Stop and remove every stack container, all profiles (volumes are kept)
	@test -f .env || { echo "no .env, nothing to stop"; exit 0; }; \
	$(COMPOSE) --profile '*' down --remove-orphans

logs: ## Follow logs (SERVICE=api to filter)
	$(COMPOSE) --profile '*' logs -f --tail=200 $(SERVICE)

ps: ## Show stack containers and their health
	$(COMPOSE) --profile '*' ps -a

test: ## Run Python and JS tests
	$(UV) run pytest --cov --cov-report=term-missing
	$(PNPM) test

evals: ## Run eval suites (WP2.10)
	@echo "make evals: not implemented yet (WP2.10: PromptFoo suites and python runners)."

lint: ## Lint Python, import contracts and JS
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run lint-imports
	$(UV) run python scripts/check_no_em_dash.py
	$(PNPM) lint

format: ## Auto-format and auto-fix Python
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy (strict on packages/core) and tsc
	$(UV) run mypy packages/core
	$(UV) run mypy packages/hire packages/pay apps sdks/python scripts
	$(PNPM) typecheck
