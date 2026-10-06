# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# Python always runs through uv (Python 3.12, see .python-version). JS runs through pnpm.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV   ?= uv
PNPM ?= pnpm

.PHONY: help install hooks dev demo test evals lint format typecheck

help: ## List targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python (uv) and JS (pnpm) dependencies
	$(UV) sync --all-packages
	$(PNPM) install

hooks: ## Install the pre-commit git hooks
	$(UV) run pre-commit install

dev: ## Start the local stack (needs docker compose from WP0.3)
	@echo "make dev: not implemented yet (WP0.3: docker compose stack)."

demo: ## Boot everything with seed data (WP0.3 infra, seeded demo in M2)
	@echo "make demo: not implemented yet (WP0.3: docker compose, seed data from M2)." >&2
	@exit 1

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
