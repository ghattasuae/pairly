// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../..");

// `pnpm dev` and the auth scripts read the repo-root .env (created by `make dev`), so the
// web app talks to the compose stack with no extra setup. Variables already set win.
// Production builds and containers get their settings from the environment only.
if (process.env.NODE_ENV !== "production") {
  try {
    process.loadEnvFile(path.join(repoRoot, ".env"));
  } catch {
    // No .env yet: settings validation reports what is missing on first request.
  }
}

/** Strictest policy: an API response may not load, run or frame anything. */
const API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  // Trace from the repo root so workspace packages end up in the standalone bundle.
  outputFileTracingRoot: repoRoot,
  turbopack: { root: repoRoot },
  experimental: {
    // No persistent Turbopack cache for `next dev` unless PAIRLY_NEXT_DEV_CACHE=1. A cache
    // left by an earlier dev session can get out of step with the code (seen on 16.4.0 and
    // 16.4.2): every HMR connection then answers with a restart of the client entry chunk
    // list, and each page reloads itself over and over (the e2e suite failed on it). A cold
    // start compiles each route once per session instead.
    turbopackFileSystemCacheForDev: process.env.PAIRLY_NEXT_DEV_CACHE === "1",
  },
  transpilePackages: ["@pairly/shared-ts"],
  poweredByHeader: false,
  // `next dev` would otherwise write AGENTS.md here (its copy contains em dashes).
  agentRules: false,
  reactStrictMode: true,
  // pg and nodemailer are Node-only; keep them out of the bundler.
  serverExternalPackages: ["pg", "nodemailer"],
  // Server actions keep the default 1 MB body limit. Policy documents (up to 25 MB) go
  // through their own route handler, /api/policies/documents, which enforces 26 MB.
  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
      // Route handlers (Better Auth, JWKS, health) return JSON or redirects, never documents
      // that load anything. Pages get their nonce-based CSP from src/proxy.ts instead; the
      // proxy never runs on /api, so the two policies never meet on one response.
      { source: "/api/:path*", headers: [{ key: "Content-Security-Policy", value: API_CSP }] },
    ];
  },
};

export default createNextIntlPlugin("./src/i18n/request.ts")(nextConfig);
