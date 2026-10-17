// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { forecastWeeks } from "./forecast";

describe("forecastWeeks", () => {
  it("keeps 1 to 26 weeks and falls back to 12 for anything else", () => {
    expect(forecastWeeks({})).toBe(12);
    expect(forecastWeeks({ weeks: "4" })).toBe(4);
    expect(forecastWeeks({ weeks: " 26 " })).toBe(26);
    for (const bad of ["0", "27", "-1", "1.5", "abc", ""]) expect(forecastWeeks({ weeks: bad })).toBe(12);
    expect(forecastWeeks({ weeks: ["3", "4"] })).toBe(12);
  });
});
