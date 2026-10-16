# ADR 0001: One monorepo, uv for Python, pnpm for JavaScript

Status: accepted

Date: 2026-10-07

## Context

Pairly is an engine, workflow plugins, three Python apps, a web app, two SDKs, evals and synthetic data. They change together: a new engine hook, the plugin that uses it, the API route, the generated TypeScript types and the web page often land in one change. The machine's system Python is too old to rely on.

## Decision

* One repository. Python is a uv workspace (`packages/*`, `apps/api`, `apps/worker`, `apps/mcp`, `sdks/python`) on Python 3.12, which uv installs. JavaScript is a pnpm workspace on Node 22 (`apps/web`, `packages/shared-ts`, `sdks/typescript`).
* One Makefile is the entry point: `make dev`, `make test`, `make lint`, `make typecheck`, `make evals`.
* Quality tools: ruff (lint and format), mypy (strict on `packages/core` and the workflow packages), import-linter contracts, pytest with coverage, eslint, tsc and vitest. pre-commit runs the fast checks.
* House rules checked by scripts: an SPDX header on every source file (`scripts/spdx_headers.py`), no em dash in docs and UI text (`scripts/check_no_em_dash.py`), English and Arabic messages in step (`scripts/check_translations.py`), AGPL-compatible dependencies only (`scripts/check_licenses.py`).

## Consequences

* A change and its tests, types and docs are reviewed together.
* The TypeScript types are generated from the API's OpenAPI document (`packages/shared-ts`), so a route change shows up as a type diff.
* Each new package must be added to the workspace lists (see [add-a-workflow.md](../add-a-workflow.md)).
