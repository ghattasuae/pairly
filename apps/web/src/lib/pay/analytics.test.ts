// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { keyTail, metricValue, payAnalyticsQuery, recordHref } from "./analytics";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

describe("pay analytics helpers", () => {
  it("links every record to a page that exists", () => {
    expect(recordHref({ kind: "invoice", id: ID })).toBe(`/pay/invoices/${ID}`);
    expect(recordHref({ kind: "bank_change", id: ID })).toBe(`/pay/bank-changes/${ID}`);
    expect(recordHref({ kind: "approval", id: ID })).toBe(`/inbox?item=${ID}`);
    expect(recordHref({ kind: "invoice", id: "../admin" })).toBeNull();
    expect(recordHref({ kind: "other", id: ID })).toBeNull();
  });

  it("reads no data as none, never as 0", () => {
    expect(metricValue({ value: null, unit: "rate" })).toEqual({ kind: "none" });
    expect(metricValue({ value: Number.NaN, unit: "rate" })).toEqual({ kind: "none" });
    expect(metricValue({ value: 0, unit: "usd" })).toEqual({ kind: "number", value: 0, unit: "usd" });
  });

  it("keeps only a valid period", () => {
    expect(payAnalyticsQuery({ since: "2026-09-01", until: "2026-09-30" })).toEqual({
      since: "2026-09-01",
      until: "2026-09-30",
    });
    expect(payAnalyticsQuery({ since: "yesterday", until: ["2026-09-30"] })).toEqual({});
    expect(payAnalyticsQuery({ since: "2026-09-30", until: "2026-09-01" })).toEqual({ since: "2026-09-30" });
    expect(keyTail("exceptions.check.bank")).toBe("check.bank");
    expect(keyTail("touchless_rate")).toBe("touchless_rate");
  });
});
