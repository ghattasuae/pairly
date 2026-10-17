// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import {
  compareLines,
  compareRows,
  failedChecks,
  findingMessage,
  reasonParts,
  severityRank,
  sortedFindings,
} from "./checks";
import type { PayCheck, PayCompareSide } from "./types";

function finding(code: string, severity: string, extra: Record<string, unknown> = {}) {
  return {
    code,
    rule: code,
    severity,
    message: { en: `${code} in English`, ar: `${code} بالعربية` },
    evidence: {},
    original_id: null,
    related_ids: [],
    needs_review: severity === "high" || severity === "critical",
    ...extra,
  };
}

const checks: PayCheck[] = [
  { check: "amounts", status: "fail", error: null, findings: [finding("round_amount", "low")] },
  { check: "bank", status: "fail", error: null, findings: [finding("bank_mismatch", "critical")] },
  { check: "forgery", status: "error", error: "document_unavailable", findings: [] },
  { check: "impersonation", status: "fail", error: null, findings: [finding("urgency_language", "medium")] },
];

function side(patch: Partial<PayCompareSide> = {}): PayCompareSide {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    reference: "AP-AAAAAA",
    status: "ready",
    document_id: "22222222-2222-4222-8222-222222222222",
    vendor_name: "Sesken Telecom",
    invoice_number: "INV-2026-01293",
    invoice_date: "2026-06-30",
    due_date: "2026-07-30",
    currency: "AED",
    po_number: null,
    subtotal: "16642.59",
    vat_amount: "832.13",
    total: "17474.72",
    lines: [{ description: "Router rental", quantity: "1", unit_price: "5862.62", line_total: "5862.62" }],
    created_at: "2026-07-01T08:00:00Z",
    ...patch,
  };
}

describe("pay checks helpers", () => {
  it("orders findings worst first and lists checks that could not run", () => {
    expect(sortedFindings(checks).map((f) => f.code)).toEqual(["bank_mismatch", "urgency_language", "round_amount"]);
    expect(sortedFindings(checks)[0]?.check).toBe("bank");
    expect(failedChecks(checks).map((c) => c.check)).toEqual(["forgery"]);
    expect(severityRank("critical")).toBeLessThan(severityRank("info"));
    expect(severityRank("unknown")).toBe(5);
  });

  it("picks the sentence in the reader's language", () => {
    const f = finding("bank_mismatch", "critical");
    expect(findingMessage(f, "ar")).toBe("bank_mismatch بالعربية");
    expect(findingMessage(f, "en")).toBe("bank_mismatch in English");
    expect(findingMessage({ message: { en: "only English" } }, "ar")).toBe("only English");
  });

  it("compares an invoice with its suspected original field by field", () => {
    const pair = {
      invoice: side({ id: "33333333-3333-4333-8333-333333333333", invoice_number: "INV-2026-01239" }),
      original: side(),
    };
    const rows = compareRows(pair);
    expect(rows.filter((r) => !r.same).map((r) => r.field)).toEqual(["invoice_number"]);
    expect(rows.find((r) => r.field === "po_number")).toEqual({ field: "po_number", invoice: null, original: null, same: true });
    const lines = compareLines({ invoice: pair.invoice, original: side({ lines: [] }) });
    expect(lines).toHaveLength(1);
    expect(lines[0]?.same).toBe(false);
    expect(compareLines(pair)[0]?.same).toBe(true);
  });
});

describe("review reasons", () => {
  it("reuses the explanation of the finding behind a check reason", () => {
    expect(reasonParts("check:bank_mismatch", checks, "en")).toEqual({
      kind: "check",
      code: "bank_mismatch",
      explanation: "bank_mismatch in English",
    });
    expect(reasonParts("check:bank_mismatch", checks, "ar").explanation).toBe("bank_mismatch بالعربية");
  });

  it("has no explanation when the finding is not in the detail", () => {
    expect(reasonParts("check:near_duplicate", checks, "en")).toEqual({
      kind: "check",
      code: "near_duplicate",
      explanation: null,
    });
    expect(reasonParts("check:bank_mismatch", [], "en").explanation).toBeNull();
  });

  it("keeps the code of the other reasons as secondary text", () => {
    expect(reasonParts("check_error:forgery:document_unavailable", checks, "en")).toEqual({
      kind: "check_error",
      code: "forgery",
      explanation: null,
    });
    expect(reasonParts("risk:critical", checks, "en")).toEqual({ kind: "risk", code: "critical", explanation: null });
    expect(reasonParts("vendor_not_found", checks, "en")).toEqual({
      kind: "vendor_not_found",
      code: null,
      explanation: null,
    });
  });
});
