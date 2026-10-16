// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { monthEndPeriod, previousPeriod } from "./month-end";

describe("monthEndPeriod", () => {
  const today = new Date("2026-10-16T10:00:00Z");
  it("reads YYYY-MM and falls back to the previous month", () => {
    expect(monthEndPeriod({ period: "2026-09" }, today)).toBe("2026-09");
    expect(monthEndPeriod({}, today)).toBe("2026-09");
    expect(monthEndPeriod({ period: "2026-13" }, today)).toBe("2026-09");
    expect(monthEndPeriod({ period: "1999-01" }, today)).toBe("2026-09");
    expect(monthEndPeriod({ period: ["2026-01"] }, today)).toBe("2026-09");
  });
  it("wraps January to December of the year before", () => {
    expect(previousPeriod(new Date("2026-01-05T00:00:00Z"))).toBe("2025-12");
  });
});
