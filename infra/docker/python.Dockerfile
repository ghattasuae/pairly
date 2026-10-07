# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# One image for the Python services. Compose picks the command:
#   migrate  alembic -c /app/apps/api/alembic.ini upgrade head   (owner role)
#   api      uvicorn pairly_api.main:app                          (app role)
#   worker   arq pairly_worker.main.WorkerSettings                (app role)
#
# Build context is the repo root:
#   docker build -f infra/docker/python.Dockerfile -t pairly-local/python-app:dev .

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.28

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- build
FROM ${PYTHON_IMAGE} AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# 1. Third-party dependencies only, from the lockfile. This layer is reused until
#    uv.lock or a pyproject.toml changes.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/hire/pyproject.toml packages/hire/
COPY packages/pay/pyproject.toml packages/pay/
COPY packages/expense/pyproject.toml packages/expense/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/
COPY apps/mcp/pyproject.toml apps/mcp/
COPY sdks/python/pyproject.toml sdks/python/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

# 2. Our own packages, installed non-editable so the runtime image needs only the venv.
COPY packages/core packages/core
COPY packages/hire packages/hire
COPY packages/pay packages/pay
COPY packages/expense packages/expense
COPY apps/api apps/api
COPY apps/worker apps/worker
COPY apps/mcp apps/mcp
COPY sdks/python sdks/python
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="pairly-python-app" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.vendor="Proofly Technology"

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Tesseract OCR (Apache-2.0) with English and Arabic data, for scanned documents and
# images (pairly_core.documents.ocr). Runs as a child of the parser sandbox.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 pairly \
    && useradd --system --uid 10001 --gid pairly --home-dir /app --shell /usr/sbin/nologin pairly

COPY --from=build --chown=root:root /app/.venv /app/.venv
# Alembic scripts are not part of the pairly-api wheel; the `migrate` service runs them.
COPY --chown=root:root apps/api/alembic.ini /app/apps/api/alembic.ini
COPY --chown=root:root apps/api/alembic /app/apps/api/alembic

WORKDIR /app
USER 10001:10001

EXPOSE 8000

# No default command on purpose: compose sets it per service.
CMD ["python", "-c", "print('Set a command: uvicorn pairly_api.main:app or arq pairly_worker.main.WorkerSettings')"]
