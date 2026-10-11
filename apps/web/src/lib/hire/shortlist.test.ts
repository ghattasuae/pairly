// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import type { Interview } from "./scheduling";
import type { ScreeningPanel } from "./screening";
import { currentInterview, decisionForKey, highlights, reasonProblem, summarize } from "./shortlist";

const interview = (status: string, starts_at: string | null): Interview =>
  ({ status, starts_at, created_at: "2026-10-01T00:00:00Z", application_id: "a" }) as unknown as Interview;

describe("shortlist helpers", () => {
  it("requires a reason for hold and reject only", () => {
    expect(reasonProblem("advance", "")).toBeNull();
    expect(reasonProblem("hold", "  ")).toBe("reasonRequired");
    expect(reasonProblem("reject", "no")).toBe("reasonShort");
    expect(reasonProblem("reject", "Not enough Kubernetes")).toBeNull();
    expect(reasonProblem("advance", "x".repeat(501))).toBe("reasonLong");
  });

  it("maps A, H and R to decisions", () => {
    expect(decisionForKey("A")).toBe("advance");
    expect(decisionForKey("h")).toBe("hold");
    expect(decisionForKey("r")).toBe("reject");
    expect(decisionForKey("x")).toBeNull();
  });

  it("summarizes the parsed CV and ignores values without evidence", () => {
    const screening = {
      profile: { experience: [{ title: { value: "Engineer" }, employer: null }], skills: [{ value: "Go" }, { nope: 1 }, { value: "Go" }] },
    } as unknown as ScreeningPanel;
    expect(summarize(screening)).toEqual({ role: "Engineer", employer: null, skills: ["Go"] });
    expect(summarize(null)).toEqual({ role: null, employer: null, skills: [] });
  });

  it("falls back to the profile's screening summary for highlights", () => {
    const profile = {
      screening: {
        status: "scored",
        overall: null,
        criteria: [
          { criterion: "AWS", result: "not_met", quotes: ["Some AWS"] },
          { criterion: "Python", result: "met", quotes: ["Eight years of Python"] },
          { criterion: "Go", result: "met", quotes: [] },
        ],
      },
    };
    expect(highlights({ screening: null, profile: profile as never })).toEqual([
      { criterion: "Python", quote: "Eight years of Python" },
      { criterion: "AWS", quote: "Some AWS" },
    ]);
  });

  it("picks the open interview before past ones", () => {
    expect(currentInterview([])).toBeNull();
    const done = interview("completed", "2026-10-02T09:00:00Z");
    const booked = interview("booked", "2026-10-09T09:00:00Z");
    expect(currentInterview([done, booked])).toBe(booked);
  });
});
