// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { CHANGE_STEPS, changeStepIndex, groupIban, isOpenTicket, looksLikeIban, looksLikeTrn, normalizeIban } from "./format";

describe("Pay format helpers", () => {
  it("normalises and groups IBANs the way they are read out", () => {
    expect(normalizeIban(" ae07-0331 2345 6789​0123 456 ")).toBe("AE070331234567890123456");
    expect(groupIban("AE070331234567890123456")).toBe("AE07 0331 2345 6789 0123 456");
    expect(looksLikeIban("ae07 0331 2345 6789 0123 456")).toBe(true);
    expect(looksLikeIban("not an iban")).toBe(false);
  });

  it("checks the TRN shape only (15 digits)", () => {
    expect(looksLikeTrn("198-4274-0184-9981")).toBe(true);
    expect(looksLikeTrn("19842740184998")).toBe(false);
    expect(looksLikeTrn("19842740184998A")).toBe(false);
  });

  it("places each ticket status on the change steps", () => {
    expect(CHANGE_STEPS).toEqual(["requested", "call", "approvals", "cooling", "active"]);
    expect(changeStepIndex("awaiting_verification")).toBe(1);
    expect(changeStepIndex("awaiting_approval")).toBe(2);
    expect(changeStepIndex("cooling_off")).toBe(3);
    expect(changeStepIndex("completed")).toBe(4);
    expect(changeStepIndex("rejected")).toBe(-1);
    expect(["awaiting_verification", "awaiting_approval", "cooling_off"].every(isOpenTicket)).toBe(true);
    expect(["completed", "rejected", "cancelled", "failed"].some(isOpenTicket)).toBe(false);
  });
});
