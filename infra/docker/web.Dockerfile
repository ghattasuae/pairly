# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
#
# Next.js web app (apps/web), pnpm workspace, standalone output.
#
# Expects apps/web/next.config.* to set:
#   output: "standalone"
#   outputFileTracingRoot: <repo root>   (so workspace packages are traced)
# With that, Next writes the server to .next/standalone/apps/web/server.js.
# apps/web/public/ must exist (keep a .gitkeep in it if it has no assets).
#
# Build context is the repo root:
#   docker build -f infra/docker/web.Dockerfile -t pairly-local/web:dev .

ARG NODE_IMAGE=node:22.23.3-alpine3.23

# ---------------------------------------------------------------- base
FROM ${NODE_IMAGE} AS base
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    NEXT_TELEMETRY_DISABLED=1 \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
# pnpm version comes from "packageManager" in the root package.json.
RUN corepack enable
WORKDIR /src

# ---------------------------------------------------------------- deps
FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/shared-ts/package.json packages/shared-ts/
COPY sdks/typescript/package.json sdks/typescript/
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --filter "@pairly/web..."

# ---------------------------------------------------------------- build
FROM deps AS build
COPY tsconfig.base.json ./
COPY packages/shared-ts packages/shared-ts
COPY sdks/typescript sdks/typescript
COPY apps/web apps/web
RUN pnpm --filter "@pairly/web..." run --if-present build

# ---------------------------------------------------------------- runtime
FROM ${NODE_IMAGE} AS runtime

LABEL org.opencontainers.image.title="pairly-web" \
      org.opencontainers.image.licenses="AGPL-3.0-only OR LicenseRef-Proofly-Commercial" \
      org.opencontainers.image.vendor="Proofly Technology"

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

WORKDIR /app

# The node image already ships a non-root `node` user (uid 1000).
COPY --from=build --chown=root:root /src/apps/web/.next/standalone ./
COPY --from=build --chown=root:root /src/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=root:root /src/apps/web/public ./apps/web/public

USER node
EXPOSE 3000

CMD ["node", "apps/web/server.js"]
