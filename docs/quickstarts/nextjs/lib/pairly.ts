// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** The Pairly client for server code (the API key never reaches the browser). */

import { Pairly } from "@pairly/sdk";

let client: Pairly | undefined;

export function pairly(): Pairly {
  client ??= new Pairly({
    apiKey: process.env["PAIRLY_API_KEY"] ?? "",
    baseUrl: process.env["PAIRLY_API_URL"] ?? "http://127.0.0.1:8000",
  });
  return client;
}

export const webUrl = (process.env["PAIRLY_WEB_URL"] ?? "http://localhost:3100").replace(/\/+$/, "");
