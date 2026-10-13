// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { analyticsQuery, metricValue, recordHref } from "./analytics";

const APP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const JOB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

describe("recordHref", () => {
  it("opens an application on its job's board and an approval in the inbox", () => {
    expect(recordHref({ kind: "application", id: APP, job_id: JOB, label: "AB12-CD34" })).toBe(
      `/hire/jobs/${JOB}/pipeline?candidate=${APP}`,
    );
    expect(recordHref({ kind: "approval", id: APP, job_id: null, label: "hire.offer" })).toBe(`/inbox/${APP}`);
  });

  it("never builds a link from something that is not an id", () => {
    expect(recordHref({ kind: "application", id: "../x", job_id: JOB, label: "x" })).toBeNull();
    expect(recordHref({ kind: "application", id: APP, job_id: "javascript:alert(1)", label: "x" })).toBeNull();
  });
});

describe("metricValue", () => {
  it("shows no data rather than a made-up zero", () => {
    expect(metricValue({ value: null, unit: "rate" })).toEqual({ kind: "none" });
    expect(metricValue({ value: 0, unit: "count" })).toEqual({ kind: "number", value: 0, unit: "count" });
  });
});

describe("analyticsQuery", () => {
  it("keeps valid filters and drops anything else", () => {
    expect(
      analyticsQuery({
        job_id: JOB,
        since: "2026-09-01",
        until: "not a date",
        ghost_days: "10",
        agreement_threshold: "150",
        minutes_per_booking: "20",
      }),
    ).toEqual({ job_id: JOB, since: "2026-09-01", ghost_days: 10, minutes_per_booking: 20 });
    expect(analyticsQuery({ job_id: "1 OR 1=1", ghost_days: "2.5" })).toEqual({});
  });
});
