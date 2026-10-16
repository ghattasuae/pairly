// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { reconciliationDays, statementFormat } from "./reconciliation";

describe("reconciliationDays", () => {
  it("reads 1 to 90 and falls back to 5", () => {
    expect(reconciliationDays({ days: "12" })).toBe(12);
    expect(reconciliationDays({ days: "90" })).toBe(90);
    expect(reconciliationDays({})).toBe(5);
    expect(reconciliationDays({ days: "0" })).toBe(5);
    expect(reconciliationDays({ days: "91" })).toBe(5);
    expect(reconciliationDays({ days: "3.5" })).toBe(5);
    expect(reconciliationDays({ days: ["7"] })).toBe(5);
  });
});

describe("statementFormat", () => {
  it("is camt.053 for XML files and CSV otherwise", () => {
    expect(statementFormat("statement-2026-10.XML")).toBe("camt053");
    expect(statementFormat("statement.csv")).toBe("csv");
    expect(statementFormat("statement")).toBe("csv");
  });
});
