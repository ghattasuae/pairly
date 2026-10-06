// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { isNavVisible, visibleNav } from "./nav";

const keys = (permissions: string[] | null) => visibleNav(permissions).map((item) => item.key);

describe("visibleNav", () => {
  it("shows only Home when the user is not provisioned", () => {
    expect(keys(null)).toEqual(["home"]);
  });

  it("shows only Home for a member with no section permissions", () => {
    expect(keys(["core.org.read", "core.decision.read"])).toEqual(["home"]);
  });

  it("shows Pay and Inbox for a pay-only membership", () => {
    expect(keys(["pay.invoice.read", "pay.invoice.approve"])).toEqual(["home", "inbox", "pay"]);
  });

  it("shows Audit to auditors and Settings to admins", () => {
    expect(keys(["core.audit.read", "core.users.read"])).toEqual(["home", "audit", "settings"]);
    expect(isNavVisible("settings", ["core.org.manage"])).toBe(true);
  });

  it("never matches a namespace by prefix only", () => {
    expect(isNavVisible("hire", ["hirex.candidate.read"])).toBe(false);
  });
});
