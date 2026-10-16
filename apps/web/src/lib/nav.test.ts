// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { isNavVisible, visibleNav } from "./nav";

const keys = (permissions: string[] | null) => visibleNav(permissions).map((item) => item.key);

describe("visibleNav", () => {
  it("shows only Home when the user is not provisioned", () => {
    expect(keys(null)).toEqual(["home"]);
  });

  it("shows Home and personal Settings for a member with no section permissions", () => {
    expect(keys(["core.org.read", "core.decision.read"])).toEqual(["home", "settings"]);
  });

  it("shows Pay and Inbox for a pay-only membership", () => {
    expect(keys(["pay.invoice.read", "pay.invoice.approve"])).toEqual(["home", "inbox", "pay", "settings"]);
  });

  it("shows the Inbox to anyone who approves in any workflow", () => {
    expect(isNavVisible("inbox", ["expense.note.approve"])).toBe(true);
  });

  it("shows Audit to auditors and Settings to admins", () => {
    expect(keys(["core.audit.read", "core.users.read"])).toEqual(["home", "audit", "settings"]);
    expect(isNavVisible("settings", ["core.org.manage"])).toBe(true);
  });

  it("shows Expenses to submitters and reviewers of expense notes, the Inbox to approvers only", () => {
    expect(keys(["expense.note.read", "expense.note.submit", "expense.document.upload"])).toEqual([
      "home",
      "expense",
      "settings",
    ]);
    expect(keys(["expense.note.read", "expense.note.approve"])).toEqual(["home", "inbox", "expense", "settings"]);
    expect(isNavVisible("expense", ["pay.invoice.read"])).toBe(false);
  });

  it("never matches a namespace by prefix only", () => {
    expect(isNavVisible("hire", ["hirex.candidate.read"])).toBe(false);
  });
});
