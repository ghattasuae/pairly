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

.PHONY: help install hooks env dev demo demo-expense down logs ps test evals lint format typecheck
.PHONY: bootstrap-org dev-seed-approval audit-anchor-now
.PHONY: ci-licenses ci-security ci-actionlint ci-test

help: ## List targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install Python (uv) and JS (pnpm) dependencies
	$(UV) sync --all-packages
	$(PNPM) install

hooks: ## Install the pre-commit git hooks
	$(UV) run pre-commit install

env: ## Create .env with random secrets, or append the keys an existing one lacks (never changes a line)
	@if [ -f .env ]; then $(UV) run python scripts/gen_dev_env.py --append-missing; \
	else $(UV) run python scripts/gen_dev_env.py; fi

# migrate, api and worker share one image (pairly-local/python-app:dev). Building it
# through `up --build` makes three services build the same tag at once, which races.
# So the shared image is built once (through `migrate`), plus the web image when the
# web profile is on, and `up` then starts everything without building.
BUILD_SERVICES := migrate $(if $(filter web,$(PROFILES)),web,)

dev: env ## Start the stack and wait until healthy (PROFILES="scan observability web")
	$(COMPOSE) $(PROFILE_ARGS) build $(BUILD_SERVICES)
	$(COMPOSE) $(PROFILE_ARGS) up -d --no-build --remove-orphans --wait --wait-timeout $(WAIT_TIMEOUT)
	@$(COMPOSE) $(PROFILE_ARGS) ps
	@echo ""
	@echo "Pairly is up. API http://localhost:8000  Mail UI http://localhost:8025  MinIO console http://localhost:9001"

demo: dev ## Boot everything healthy (seed data arrives in M2)
	@echo ""
	@echo "make demo: the stack is healthy. Seeded demo data arrives in M2, so there is nothing to explore yet."

# The expense note example workflow (docs/add-a-workflow.md), offline in recorded mode.
# Needs an organisation (make bootstrap-org). Seeds its policies, three demo people
# (invitation links are printed once) and the recorded model responses, and checks
# that the synthetic receipts in synth/expense/receipts are current.
demo-expense: ## Seed the expense example in an org (SLUG=acme): policies, people, recordings
	@if [ -z "$(SLUG)" ]; then echo 'usage: make demo-expense SLUG=acme'; exit 2; fi
	$(UV) run python -m synth.expense --check
	$(COMPOSE) run --rm --no-deps migrate pairly-admin seed expense-demo --org "$(SLUG)"
	@echo ""
	@echo "Upload a receipt from synth/expense/receipts as the submitter (POST /v1/expense/notes),"
	@echo "then decide it as the approver from the inbox or the email link (Mail UI on :8025)."

down: ## Stop and remove every stack container, all profiles (volumes are kept)
	@test -f .env || { echo "no .env, nothing to stop"; exit 0; }; \
	$(COMPOSE) --profile '*' down --remove-orphans

# Runs in the migrate image, the only one that gets the owner database role.
bootstrap-org: ## Create an org and print its admin invite link (NAME, SLUG, ADMIN_EMAIL)
	@if [ -z "$(NAME)" ] || [ -z "$(SLUG)" ] || [ -z "$(ADMIN_EMAIL)" ]; then \
		echo 'usage: make bootstrap-org NAME="Acme Ltd" SLUG=acme ADMIN_EMAIL=you@acme.test'; exit 2; fi
	$(COMPOSE) run --rm --no-deps migrate pairly-admin bootstrap-org \
		--name "$(NAME)" --slug "$(SLUG)" --admin-email "$(ADMIN_EMAIL)"

dev-seed-approval: ## Development only: one pending approval for the web e2e suite (ORG, EMAIL, ARGS)
	@if [ -z "$(ORG)" ] || [ -z "$(EMAIL)" ]; then \
		echo 'usage: make dev-seed-approval ORG=<org slug> EMAIL=<approver email> [ARGS="--link approve --decision"]'; exit 2; fi
	$(COMPOSE) run --rm --no-deps -e PAIRLY_DEVSEED=1 migrate python -m pairly_api.devseed approval \
		--org-slug "$(ORG)" --approver-email "$(EMAIL)" $(ARGS)

audit-anchor-now: ## Development only: sign and store every org's audit chain head now (normally nightly)
	$(COMPOSE) exec -T worker python -m pairly_worker.run_now audit_anchor_nightly

logs: ## Follow logs (SERVICE=api to filter)
	$(COMPOSE) --profile '*' logs -f --tail=200 $(SERVICE)

ps: ## Show stack containers and their health
	$(COMPOSE) --profile '*' ps -a

test: ## Run Python and JS tests
	$(UV) run --frozen pytest --cov --cov-report=term-missing
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

typecheck: ## mypy (strict on packages/core and packages/expense) and tsc
	$(UV) run mypy packages/core packages/expense
	$(UV) run mypy packages/hire packages/pay apps sdks/python scripts synth
	$(PNPM) typecheck

# ---------------------------------------------------------------- CI checks, run locally
# Same tools and pinned versions as .github/workflows/ci.yml.
BANDIT_VERSION   ?= 1.9.4
PIP_AUDIT_VERSION ?= 2.10.1

ci-licenses: ## License gate: every dependency must be AGPL compatible
	$(UV) run --frozen python scripts/check_licenses.py

ci-security: ## Bandit, pip-audit, pnpm audit and gitleaks, as in CI
	uvx --from bandit==$(BANDIT_VERSION) bandit -c scripts/bandit.yaml \
		--severity-level medium --confidence-level medium -r packages apps sdks/python scripts
	@tmp="$$(mktemp -d)"; trap 'rm -rf "$$tmp"' EXIT; \
	$(UV) export --frozen --all-packages --no-emit-workspace --format requirements.txt \
		-o "$$tmp/requirements.txt" -q && \
	uvx pip-audit==$(PIP_AUDIT_VERSION) -r "$$tmp/requirements.txt" \
		--disable-pip --require-hashes --progress-spinner off
	$(PNPM) audit --prod --audit-level high
	$(PNPM) audit --audit-level critical
	"$$(scripts/ci_tools.sh gitleaks)" git --redact --no-banner .

ci-actionlint: ## Lint the GitHub Actions workflows
	"$$(scripts/ci_tools.sh actionlint)"

ci-test: ## pytest with coverage against a throwaway Postgres, as in CI
	@db_env="$$(scripts/ci_postgres.sh start)" && eval "$$db_env" || exit 1; trap 'scripts/ci_postgres.sh stop' EXIT; \
	$(UV) run --frozen alembic -c apps/api/alembic.ini upgrade heads && \
	$(UV) run --frozen alembic -c apps/api/alembic.ini check && \
	$(UV) run --frozen pytest --cov --cov-report=term-missing
