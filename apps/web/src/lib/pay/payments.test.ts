// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import {
  chainNames,
  exportFilename,
  fromMinor,
  parseReason,
  rulesLines,
  runSums,
  runStatusFilter,
  submitState,
  toMinor,
} from "./payments";

describe("parseReason", () => {
  it("splits code and detail", () => {
    expect(parseReason("vendor_held:manual")).toMatchObject({ code: "vendor_held", detail: "manual", item: null });
    expect(parseReason("checks_stale")).toMatchObject({ code: "checks_stale", detail: null });
  });

  it("reads the bank file item in front", () => {
    expect(parseReason("PR-AB12CD-0001:bank_details_changed")).toMatchObject({
      item: "PR-AB12CD-0001",
      code: "bank_details_changed",
    });
  });

  it("keeps unknown reasons as they are", () => {
    expect(parseReason("something_else:x")).toMatchObject({ code: null, raw: "something_else:x" });
  });
});

describe("submitState", () => {
  it("allows a ready invoice with current checks and no blockers", () => {
    expect(submitState({ status: "ready", checks_state: "current", blockers: [] })).toEqual({ can: true, why: [] });
  });

  it("refuses stale checks, blockers and other statuses", () => {
    expect(submitState({ status: "ready", checks_state: "stale", blockers: [] })).toEqual({
      can: false,
      why: ["checks_stale"],
    });
    expect(submitState({ status: "ready", checks_state: "current", blockers: ["vendor_held:manual"] }).can).toBe(false);
    expect(submitState({ status: "ready", checks_state: "stale", blockers: ["checks_stale"] }).why).toEqual([
      "checks_stale",
    ]);
    expect(submitState({ status: "needs_review", checks_state: "current", blockers: [] }).can).toBe(false);
    expect(submitState({ status: "ready" }).why).toEqual(["checks_missing"]);
  });
});

describe("money in minor units", () => {
  it("adds without float drift", () => {
    expect(toMinor("0.1")).toBe(10n);
    expect(toMinor("1200.505")).toBe(120050n);
    expect(toMinor("abc")).toBeNull();
    expect(fromMinor(toMinor("0.1")! + toMinor("0.2")!)).toBe("0.30");
    expect(fromMinor(-5n)).toBe("-0.05");
  });

  it("sums paid and unpaid items", () => {
    expect(
      runSums([
        { status: "included", amount: "100.10" },
        { status: "paid", amount: "0.20" },
        { status: "released", amount: "999" },
        { status: "included", amount: "0.20" },
      ]),
    ).toEqual({ unpaid: "100.30", paid: "0.20" });
  });
});

describe("filters and names", () => {
  it("reads the run status filter", () => {
    expect(runStatusFilter("paid")).toBe("paid");
    expect(runStatusFilter(["exported", "paid"])).toBe("exported");
    expect(runStatusFilter("bogus")).toBeNull();
  });

  it("makes a safe file name", () => {
    expect(exportFilename("PR-AB12/CD", "pain001", 2)).toBe("PR-AB12CD-2.xml");
    expect(exportFilename("PR-X", "csv", 1)).toBe("PR-X-1.csv");
  });

  it("lists the chains of a rules document and marks comments", () => {
    const yaml = [
      "# who approves",
      "schema: pairly.approval_rules/v1",
      "chains:",
      "  - name: high_risk",
      "    when:",
      "      - {field: risk_level, op: in, value: [high]}",
      "    steps:",
      "      - {name: approver, roles: [approver]}",
      "  - name: standard",
      "    steps:",
      "      - {name: approver, roles: [approver]}",
    ].join("\n");
    expect(chainNames(yaml)).toEqual(["high_risk", "standard"]);
    expect(rulesLines(yaml)[0]).toEqual({ text: "# who approves", comment: true });
    expect(rulesLines(yaml)[1]?.comment).toBe(false);
  });
});
