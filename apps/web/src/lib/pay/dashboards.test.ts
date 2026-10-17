// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { payActionTotal, payDashboardsFor, safeHref, type PayDashboard } from "./dashboards";

const ALL = ["pay.invoice.process", "pay.invoice.approve", "pay.analytics.read"];

describe("payDashboardsFor", () => {
  it("picks by role, in order, for Pay or every workflow", () => {
    expect(payDashboardsFor([{ role: "finance_controller" }, { role: "ap_clerk", workflow: "pay" }], ALL)).toEqual([
      "ap_clerk",
      "finance_controller",
    ]);
    expect(payDashboardsFor([{ role: "approver", workflow: "hire" }], ALL)).toEqual([]);
    expect(payDashboardsFor([{ role: "recruiter" }, { role: "admin" }], ALL)).toEqual([]);
  });
  it("needs the permission the API checks", () => {
    expect(payDashboardsFor([{ role: "approver" }], ["pay.invoice.read"])).toEqual([]);
    expect(payDashboardsFor([{ role: "approver" }], ["pay.invoice.approve"])).toEqual(["approver"]);
  });
});

describe("helpers", () => {
  it("adds up what needs the person and keeps links inside the app", () => {
    const board = { action: [{ count: 2 }, { count: 3 }] } as unknown as PayDashboard;
    expect(payActionTotal([board, board])).toBe(10);
    expect(safeHref("/pay/invoices/x")).toBe("/pay/invoices/x");
    expect(safeHref("//evil.example")).toBeNull();
    expect(safeHref("https://evil.example")).toBeNull();
    expect(safeHref(null)).toBeNull();
  });
});
