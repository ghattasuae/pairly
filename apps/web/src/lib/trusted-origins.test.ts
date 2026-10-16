// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { errorKeyFor } from "./auth/sign-in-errors";
import { parseTrustedOrigins } from "./env";

describe("parseTrustedOrigins", () => {
  it("accepts exact origins and drops blanks and duplicates", () => {
    expect(parseTrustedOrigins(undefined)).toEqual([]);
    expect(parseTrustedOrigins(" http://127.0.0.1:3000 , https://pairly.example/, ,http://127.0.0.1:3000")).toEqual([
      "http://127.0.0.1:3000",
      "https://pairly.example",
    ]);
  });

  it.each([
    "*.pairly.example",
    "https://*.pairly.example",
    "http://localhost:3000/app",
    "http://localhost:3000?x=1",
    "http://user:pw@localhost:3000",
    "ftp://localhost",
    "localhost:3000",
    "javascript:alert(1)",
  ])("rejects %s", (entry) => {
    expect(() => parseTrustedOrigins(entry)).toThrow(/PAIRLY_AUTH_TRUSTED_ORIGINS/);
  });
});

describe("sign-in error for an untrusted origin", () => {
  it("is explained in development only", () => {
    const rejected = { status: 403, code: "INVALID_ORIGIN" };
    expect(errorKeyFor(rejected, true)).toBe("originMismatch");
    expect(errorKeyFor(rejected, false)).toBe("generic");
    expect(errorKeyFor({ status: 429 }, false)).toBe("rateLimited");
  });
});
