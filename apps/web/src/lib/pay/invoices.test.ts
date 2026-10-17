// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import {
  displayValue,
  firstToReview,
  groupFields,
  highlightId,
  moveIndex,
  needsLook,
  percent,
  reasonKey,
  reviewOrder,
  statusFilter,
  textPieces,
  type ReviewField,
} from "./invoices";

function field(path: string, extra: Partial<ReviewField> = {}): ReviewField {
  return {
    path,
    pattern: path.replace(/\[\d+\]/g, "[]"),
    kind: "text",
    value: "v",
    current_value: "v",
    raw_value: null,
    confidence: 0.95,
    threshold: 0.8,
    required: false,
    needs_correction: false,
    corrected: false,
    flags: [],
    spans: [],
    ...extra,
  };
}

describe("invoice review helpers", () => {
  it("groups header, bank and line items in reading order", () => {
    const groups = groupFields([
      field("line_items[1].line_total"),
      field("total"),
      field("bank.swift"),
      field("line_items[0].description"),
      field("line_items[1].description"),
      field("vendor_name"),
      field("bank.iban"),
    ]);
    expect(groups.header.map((f) => f.path)).toEqual(["vendor_name", "total"]);
    expect(groups.bank.map((f) => f.path)).toEqual(["bank.iban", "bank.swift"]);
    expect(groups.lines.map((l) => [l.index, l.fields.map((f) => f.path)])).toEqual([
      [0, ["line_items[0].description"]],
      [1, ["line_items[1].description", "line_items[1].line_total"]],
    ]);
    expect(reviewOrder(groups).map((f) => f.path)[0]).toBe("vendor_name");
  });

  it("starts the review at the first doubtful field", () => {
    const order = [field("a"), field("b", { needs_correction: true }), field("c", { needs_correction: true, corrected: true })];
    expect(needsLook(order[1]!)).toBe(true);
    expect(needsLook(order[2]!)).toBe(false);
    expect(firstToReview(order)).toBe(1);
    expect(firstToReview([field("a")])).toBe(0);
  });

  it("moves with arrows, j and k, Home and End, inside the list", () => {
    expect(moveIndex("ArrowDown", 0, 3)).toBe(1);
    expect(moveIndex("j", 2, 3)).toBe(2);
    expect(moveIndex("ArrowUp", 0, 3)).toBe(0);
    expect(moveIndex("k", 2, 3)).toBe(1);
    expect(moveIndex("Home", 2, 3)).toBe(0);
    expect(moveIndex("End", 0, 3)).toBe(2);
    expect(moveIndex("x", 0, 3)).toBeNull();
    expect(moveIndex("ArrowDown", 0, 0)).toBeNull();
  });

  it("splits a text by evidence offsets and ignores bad ones", () => {
    const text = "Invoice No: 7\nTotal: 10";
    const pieces = textPieces(text, [
      field("invoice_number", { spans: [{ start: 0, end: 13, quote: "Invoice No: 7" }] }),
      field("total", { spans: [{ start: 14, end: 23 }, { start: 5, end: 999 }, { start: "x" as unknown as number, end: 2 }] }),
    ]);
    expect(pieces.map((p) => p.text).join("")).toBe(text);
    expect(pieces.find((p) => p.text === "Invoice No: 7")?.paths).toEqual(["invoice_number"]);
    expect(pieces.find((p) => p.text === "Total: 10")?.paths).toEqual(["total"]);
    expect(pieces.find((p) => p.text === "\n")?.paths).toEqual([]);
  });

  it("formats values, confidence, ids, reasons and the status filter", () => {
    expect(displayValue(null)).toBe("");
    expect(displayValue("6507.27")).toBe("6507.27");
    expect(displayValue({ amount: "1.00", currency: "AED" })).toBe("AED 1.00");
    expect(percent(0.956)).toBe(96);
    expect(percent(4)).toBe(100);
    expect(highlightId("total")).toBe("field:total:0");
    expect(reasonKey("extraction:low_confidence:total")).toBe("extraction");
    expect(reasonKey("vendor_not_found")).toBe("vendor_not_found");
    expect(statusFilter("needs_review")).toBe("needs_review");
    expect(statusFilter(["ready", "paid"])).toBe("ready");
    expect(statusFilter("nope")).toBeNull();
    expect(statusFilter(undefined)).toBeNull();
  });
});
