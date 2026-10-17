// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { acceptHigh, applySuggestion, changesOf, draftsOf, percent } from "./coding";
import type { PayCodingSuggestion, PayLineCoding } from "./types";

function suggestion(patch: Partial<PayCodingSuggestion> = {}): PayCodingSuggestion {
  return {
    gl_code: "6100",
    cost_centre: "CC-ADM",
    confidence: 0.92,
    level: "high",
    method: "history",
    support: 2,
    examples: ["AP-1"],
    reason: null,
    ...patch,
  };
}

function line(index: number, patch: Partial<PayLineCoding> = {}): PayLineCoding {
  return {
    line_index: index,
    description: `Line ${index}`,
    gl_code: null,
    cost_centre: null,
    source: null,
    corrected: false,
    coded_at: null,
    suggestion: null,
    ...patch,
  };
}

describe("GL coding drafts", () => {
  it("start from the stored codes", () => {
    const drafts = draftsOf([line(0, { gl_code: "6200", cost_centre: "CC-IT" }), line(2)]);
    expect(drafts).toEqual({ 0: { gl_code: "6200", cost_centre: "CC-IT" }, 2: { gl_code: "", cost_centre: "" } });
  });

  it("apply a suggestion only when asked, keeping a field it leaves empty", () => {
    expect(applySuggestion({ gl_code: "", cost_centre: "CC-IT" }, suggestion({ cost_centre: null }))).toEqual({
      gl_code: "6100",
      cost_centre: "CC-IT",
    });
    expect(applySuggestion({ gl_code: "1500", cost_centre: "" }, null)).toEqual({ gl_code: "1500", cost_centre: "" });
  });

  it("use every high confidence suggestion on uncoded lines only", () => {
    const lines = [
      line(0, { suggestion: suggestion() }),
      line(1, { suggestion: suggestion({ level: "medium", gl_code: "6200" }) }),
      line(2, { gl_code: "1500", suggestion: suggestion() }),
    ];
    const next = acceptHigh(lines, draftsOf(lines));
    expect(next[0]).toEqual({ gl_code: "6100", cost_centre: "CC-ADM" });
    expect(next[1]).toEqual({ gl_code: "", cost_centre: "" });
    expect(next[2]).toEqual({ gl_code: "1500", cost_centre: "" });
  });

  it("send only the lines that changed, empty as null", () => {
    const lines = [line(0, { gl_code: "6100" }), line(1), line(2, { gl_code: "6200", cost_centre: "CC-IT" })];
    const drafts = {
      0: { gl_code: "6100", cost_centre: "" },
      1: { gl_code: "6100", cost_centre: "CC-ADM" },
      2: { gl_code: "", cost_centre: "CC-IT" },
    };
    expect(changesOf(lines, drafts)).toEqual([
      { line_index: 1, gl_code: "6100", cost_centre: "CC-ADM" },
      { line_index: 2, gl_code: null, cost_centre: "CC-IT" },
    ]);
  });

  it("show confidence as a whole percentage", () => {
    expect(percent(0.666)).toBe(67);
    expect(percent(1.2)).toBe(100);
    expect(percent(-1)).toBe(0);
  });
});
