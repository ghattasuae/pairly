// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { EXCEPTION_CATEGORIES, ageParts, isCategory, itemsOf, move, typingIn } from "./exceptions";
import type { PayExceptionItem } from "./types";

function item(reference: string, categories: string[]): PayExceptionItem {
  return {
    id: reference,
    reference,
    status: "needs_review",
    vendor_name: null,
    invoice_number: null,
    po_number: null,
    currency: "AED",
    total: "10.00",
    created_at: "2026-10-14T08:00:00Z",
    age_hours: 3,
    overdue: false,
    categories: categories.map((category) => ({
      category: category as PayExceptionItem["categories"][number]["category"],
      explanations: [],
      resolution: { en: "Do this.", ar: "افعل هذا." },
      templates: [],
    })),
  };
}

describe("exceptions queue", () => {
  it("knows the nine categories", () => {
    expect(EXCEPTION_CATEGORIES).toHaveLength(9);
    expect(isCategory("missing_receipt")).toBe(true);
    expect(isCategory("anything")).toBe(false);
    expect(isCategory(null)).toBe(false);
  });

  it("filters by category", () => {
    const items = [item("A", ["no_po"]), item("B", ["price_variance", "missing_receipt"]), item("C", ["other"])];
    expect(itemsOf(items, "missing_receipt").map((i) => i.reference)).toEqual(["B"]);
    expect(itemsOf(items, null)).toHaveLength(3);
  });

  it("moves with J and K inside the list", () => {
    expect(move(0, "j", 3)).toBe(1);
    expect(move(2, "j", 3)).toBe(2);
    expect(move(1, "k", 3)).toBe(0);
    expect(move(0, "K", 3)).toBe(0);
    expect(move(5, "x", 3)).toBe(2);
    expect(move(0, "j", 0)).toBe(-1);
    expect(move(0, "ArrowDown", 2)).toBe(1);
  });

  it("splits the age into days and hours", () => {
    expect(ageParts(53)).toEqual({ days: 2, hours: 5 });
    expect(ageParts(4.9)).toEqual({ days: 0, hours: 4 });
    expect(ageParts(-3)).toEqual({ days: 0, hours: 0 });
  });

  it("leaves shortcuts off while typing", () => {
    expect(typingIn(document.createElement("textarea"))).toBe(true);
    expect(typingIn(document.createElement("input"))).toBe(true);
    expect(typingIn(document.createElement("li"))).toBe(false);
    expect(typingIn(null)).toBe(false);
  });
});
