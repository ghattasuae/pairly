// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Me } from "@pairly/shared-ts";
import { describe, expect, it } from "vitest";

import { classifyMeResponse, secondFactorNeed } from "./viewer-state";

const me = {
  id: "7d6f1d1e-1111-4111-8111-111111111111",
  email: "sam@example.test",
  display_name: "Sam",
  org: { id: "7d6f1d1e-2222-4222-8222-222222222222", name: "Acme", slug: "acme", sandbox: false },
  roles: [{ role: "admin", workflow: null }],
  permissions: ["core.org.manage"],
  second_factor: false,
  second_factor_permissions: ["core.org.manage"],
} satisfies Me;

describe("classifyMeResponse", () => {
  it("is ready on 200", () => {
    expect(classifyMeResponse(200, me, undefined)).toEqual({ status: "ready", me });
  });

  it("is not provisioned on 403 and keeps the problem code", () => {
    expect(classifyMeResponse(403, undefined, { code: "forbidden" })).toEqual({
      status: "not_provisioned",
      code: "forbidden",
    });
  });

  it("treats 401 as unauthenticated", () => {
    expect(classifyMeResponse(401, undefined, {})).toEqual({ status: "unauthenticated" });
  });

  it("fails closed on anything else", () => {
    expect(classifyMeResponse(500, undefined, {})).toEqual({ status: "unavailable" });
    expect(classifyMeResponse(200, undefined, undefined)).toEqual({ status: "unavailable" });
    expect(classifyMeResponse(undefined, undefined, undefined)).toEqual({ status: "unavailable" });
  });
});

describe("secondFactorNeed", () => {
  it("asks people who can manage or approve to set up TOTP, or to sign in again", () => {
    expect(secondFactorNeed(me, false)).toBe("setup");
    expect(secondFactorNeed(me, true)).toBe("reauth");
  });

  it("asks nothing once the session has TOTP, or when no permission needs it", () => {
    expect(secondFactorNeed({ ...me, second_factor: true }, false)).toBeNull();
    expect(secondFactorNeed({ ...me, second_factor_permissions: [] }, false)).toBeNull();
  });
});
