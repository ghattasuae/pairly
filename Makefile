# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# Python always runs through uv (Python 3.12, see .python-version). JS runs through pnpm.
#
# Two ways to run the stack:
# - Native (the default for development and tests): `make dev` / `make local-up`. Postgres
#   16 and Valkey run as local processes with their data in .local/, and the api, worker
#   and web run as plain processes (scripts/local_stack.sh). No containers.
# - Docker Compose (for CI and self-hosters): `make docker-dev` (infra/docker-compose.yml).
# The admin targets (bootstrap-org, demo-expense, demo-hire, dev-seed-approval, audit-anchor-now)
# run against the native stack; STACK=docker runs them in the compose stack instead.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV   ?= uv
PNPM ?= pnpm

# native (default) or docker: which stack the admin targets talk to.
STACK ?= native
LOCAL := scripts/local_stack.sh

# Docker Compose stack (infra/docker-compose.yml). Extra profiles: web, scan, observability.
#   PROFILES="scan observability" make docker-dev
PROFILES     ?=
COMPOSE_FILE := infra/docker-compose.yml
COMPOSE      := docker compose --env-file .env -f $(COMPOSE_FILE)
PROFILE_ARGS := $(foreach p,$(PROFILES),--profile $(p))
# Seconds `make docker-dev` waits for every service to report healthy.
WAIT_TIMEOUT ?= 300
# Optional service filter for `make logs` / `make docker-logs`, e.g. SERVICE=api
SERVICE      ?=

ifeq ($(STACK),docker)
RUN_OWNER  := $(COMPOSE) run --rm --no-deps migrate
RUN_SEED   := $(COMPOSE) run --rm --no-deps -e PAIRLY_DEVSEED=1 migrate
RUN_WORKER := $(COMPOSE) exec -T worker
else ifeq ($(STACK),native)
RUN_OWNER  := $(LOCAL) exec owner --
RUN_SEED   := LOCAL_EXTRA_ENV=PAIRLY_DEVSEED=1 $(LOCAL) exec owner --
RUN_WORKER := $(LOCAL) exec worker --
else
$(error STACK must be native or docker)
endif

.PHONY: help install hooks env dev demo demo-expense demo-hire down logs ps test evals lint format typecheck
.PHONY: local-up local-down local-status local-test local-logs
.PHONY: docker-dev docker-demo docker-down docker-logs docker-ps
.PHONY: bootstrap-org dev-seed-approval dev-seed-hire-screening audit-anchor-now inbound-drop
.PHONY: ci-licenses ci-security ci-actionlint ci-test
.PHONY: mcp api-docs sandbox

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

# ---------------------------------------------------------------- native stack (default)
dev: local-up ## Start the native stack (Postgres, Valkey, api, worker, web on :3100), no containers

demo: local-up ## Native stack up plus a complete Hire, Pay and expense demo (org "demo"), offline
	$(UV) run python -m synth.hire --check
	$(UV) run python -m pairly_hire.recordings --check
	$(UV) run python -m pairly_hire.messaging.recordings --check
	$(UV) run python -m pairly_hire.interviewing.recordings --check
	$(UV) run python -m pairly_hire.analytics.calibration_recordings --check
	$(UV) run python -m synth.hire.interviews --check
	$(UV) run python -m synth.hire.references --check
	$(UV) run python -m synth.expense --check
	$(UV) run python -m synth.pay --check
	$(UV) run python -m pairly_pay.ai_recordings --check
	nice -n 10 $(LOCAL) exec demo -- python scripts/demo_seed.py

local-up: env ## Start Postgres, Valkey, migrations, api, worker and web as local processes
	$(LOCAL) up

local-down: ## Stop every native process (web, worker, api, Valkey, Postgres)
	$(LOCAL) down

local-status: ## Show the native processes and their health
	$(LOCAL) status

local-test: ## pytest with coverage against fresh local test databases (no Docker)
	$(LOCAL) test

local-logs: ## Follow the native logs (SERVICE=api|worker|web to filter)
	$(LOCAL) logs $(SERVICE)

down: local-down ## Stop the native stack (make docker-down for the compose stack)

logs: local-logs ## Follow the native logs (SERVICE=api to filter)

ps: local-status ## Show the native processes and their health

# ---------------------------------------------------------------- Docker Compose stack
# For CI and self-hosters. migrate, api and worker share one image
# (pairly-local/python-app:dev). Building it through `up --build` makes three services
# build the same tag at once, which races. So the shared image is built once (through
# `migrate`), plus the web image when the web profile is on, and `up` then starts
# everything without building.
BUILD_SERVICES := migrate $(if $(filter web,$(PROFILES)),web,)

docker-dev: env ## Compose stack, wait until healthy (PROFILES="scan observability web")
	$(COMPOSE) $(PROFILE_ARGS) build $(BUILD_SERVICES)
	$(COMPOSE) $(PROFILE_ARGS) up -d --no-build --remove-orphans --wait --wait-timeout $(WAIT_TIMEOUT)
	@$(COMPOSE) $(PROFILE_ARGS) ps
	@echo ""
	@echo "Pairly is up. API http://localhost:8000  Mail UI http://localhost:8025  MinIO console http://localhost:9001"

docker-demo: docker-dev ## Compose stack healthy; seed with STACK=docker make bootstrap-org / demo-expense
	@echo ""
	@echo "make docker-demo: the stack is healthy. Seed it with STACK=docker make bootstrap-org"
	@echo "and STACK=docker make demo-expense SLUG=<slug>."

docker-down: ## Stop and remove every compose container, all profiles (volumes are kept)
	@test -f .env || { echo "no .env, nothing to stop"; exit 0; }; \
	$(COMPOSE) --profile '*' down --remove-orphans

docker-logs: ## Follow compose logs (SERVICE=api to filter)
	$(COMPOSE) --profile '*' logs -f --tail=200 $(SERVICE)

docker-ps: ## Show compose containers and their health
	$(COMPOSE) --profile '*' ps -a

# ---------------------------------------------------------------- admin (STACK=native|docker)
# The expense note example workflow (docs/add-a-workflow.md), offline in recorded mode.
# Needs an organisation (make bootstrap-org). Seeds its policies, three demo people
# (invitation links are printed once) and the recorded model responses, and checks
# that the synthetic receipts in synth/expense/receipts are current.
demo-hire: ## Seed Pairly Hire in an org (SLUG=acme): jobs, rubrics, people, recordings
	@if [ -z "$(SLUG)" ]; then echo 'usage: make demo-hire SLUG=acme'; exit 2; fi
	$(UV) run python -m pairly_hire.recordings --check
	$(UV) run python -m pairly_hire.messaging.recordings --check
	$(UV) run python -m pairly_hire.interviewing.recordings --check
	$(UV) run python -m pairly_hire.analytics.calibration_recordings --check
	$(RUN_OWNER) pairly-admin seed hire-demo --org "$(SLUG)"
	@echo ""
	@echo "Careers page: /en/careers/$(SLUG) and /ar/careers/$(SLUG) on the web app."

demo-expense: ## Seed the expense example in an org (SLUG=acme): policies, people, recordings
	@if [ -z "$(SLUG)" ]; then echo 'usage: make demo-expense SLUG=acme'; exit 2; fi
	$(UV) run python -m synth.expense --check
	$(RUN_OWNER) pairly-admin seed expense-demo --org "$(SLUG)"
	@echo ""
	@echo "Upload a receipt from synth/expense/receipts as the submitter (POST /v1/expense/notes),"
	@echo "then decide it as the approver from the inbox or the email link (native: .local/mail;"
	@echo "Docker: Mail UI on :8025)."

# Runs as the owner database role (natively, or in the migrate image with STACK=docker).
bootstrap-org: ## Create an org and print its admin invite link (NAME, SLUG, ADMIN_EMAIL)
	@if [ -z "$(NAME)" ] || [ -z "$(SLUG)" ] || [ -z "$(ADMIN_EMAIL)" ]; then \
		echo 'usage: make bootstrap-org NAME="Acme Ltd" SLUG=acme ADMIN_EMAIL=you@acme.test'; exit 2; fi
	$(RUN_OWNER) pairly-admin bootstrap-org \
		--name "$(NAME)" --slug "$(SLUG)" --admin-email "$(ADMIN_EMAIL)"

dev-seed-approval: ## Development only: one pending approval for the web e2e suite (ORG, EMAIL, ARGS)
	@if [ -z "$(ORG)" ] || [ -z "$(EMAIL)" ]; then \
		echo 'usage: make dev-seed-approval ORG=<org slug> EMAIL=<approver email> [ARGS="--link approve --decision"]'; exit 2; fi
	$(RUN_SEED) python -m pairly_api.devseed approval \
		--org-slug "$(ORG)" --approver-email "$(EMAIL)" $(ARGS)

dev-seed-hire-screening: ## Development only: the screening e2e job, recordings and synthetic CVs (ORG, OUT, COUNT)
	@if [ -z "$(ORG)" ] || [ -z "$(OUT)" ]; then \
		echo 'usage: make dev-seed-hire-screening ORG=<org slug> OUT=<directory> [COUNT=20]'; exit 2; fi
	$(RUN_SEED) python scripts/e2e_hire_screening_seed.py \
		--org-slug "$(ORG)" --out "$(OUT)" --count "$(or $(COUNT),20)"

# There is no mail receiver in development: this hands an .eml file to Pay's email intake
# as if it had been mailed to the address in its To header (pairly_pay.inbound_drop). The
# worker also delivers files dropped in .local/inbound/ every minute. Native stack only;
# refused outside PAIRLY_ENV=development and test.
inbound-drop: ## Development only: deliver an .eml to the org whose inbound address is in its To header (FILE)
	@if [ -z "$(FILE)" ]; then echo 'usage: make inbound-drop FILE=<path to .eml>'; exit 2; fi
	@test -f "$(FILE)" || { echo "no such file: $(FILE)"; exit 2; }
	$(LOCAL) exec worker -- python -m pairly_pay.inbound_drop "$(abspath $(FILE))"

audit-anchor-now: ## Development only: sign and store every org's audit chain head now (normally nightly)
	$(RUN_WORKER) python -m pairly_worker.run_now audit_anchor_nightly

# ---------------------------------------------------------------- developer surface (M6)
sandbox: ## Create a sandbox org with demo data and print a pk_test_ key (NAME, SLUG, ADMIN_EMAIL optional)
	$(RUN_OWNER) pairly-admin sandbox create --name "$(or $(NAME),Pairly sandbox)" \
		$(if $(SLUG),--slug "$(SLUG)",) $(if $(ADMIN_EMAIL),--admin-email "$(ADMIN_EMAIL)",)

mcp: ## Run the Pairly MCP server on stdio (PAIRLY_API_KEY or PAIRLY_TOKEN+PAIRLY_ORG; apps/mcp/README.md)
	@if [ -z "$$PAIRLY_API_KEY" ] && [ -z "$$PAIRLY_TOKEN" ]; then \
		echo 'usage: PAIRLY_API_KEY=pk_test_... make mcp  (or PAIRLY_TOKEN=... PAIRLY_ORG=...)'; exit 2; fi
	@$(UV) run --quiet --package pairly-mcp pairly-mcp

test: ## Run Python and JS tests
	$(UV) run --frozen pytest --cov --cov-report=term-missing
	$(PNPM) test

evals: ## Run the eval suites (guardrails, Hire screening, Pay checks) and the threshold gate
	nice -n 10 $(UV) run python -m evals.guardrails_hire
	nice -n 10 $(UV) run python -m evals.hire
	nice -n 10 $(UV) run python -m evals.pay
	$(UV) run python -m evals.gate

lint: ## Lint Python, import contracts and JS
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run lint-imports
	$(UV) run python scripts/check_no_em_dash.py
	$(UV) run python scripts/check_translations.py
	$(PNPM) lint

format: ## Auto-format and auto-fix Python
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy (strict on packages/core, packages/expense, packages/hire and packages/pay) and tsc
	$(UV) run mypy packages/core packages/expense packages/hire packages/pay
	$(UV) run mypy apps sdks/python scripts synth evals
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
