// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { actionTotal, dashboardsFor, itemHref, sectionHref, type Dashboard, type DashItem } from "./dashboards";

const JOB = "11111111-1111-4111-8111-111111111111";
const APP = "22222222-2222-4222-8222-222222222222";

const RECRUITER = ["hire.candidate.read", "hire.pipeline.write", "hire.analytics.read", "hire.interview.read", "hire.referral.read"];

function item(kind: string, extra: Partial<DashItem> = {}): DashItem {
  return { kind, id: "x-1", label: "Candidate H-1", job_id: JOB, application_id: APP, at: null, detail: {}, stages: [], ...extra };
}

describe("dashboardsFor", () => {
  it("picks the dashboards of the person's Hire roles, in a stable order", () => {
    expect(dashboardsFor([{ role: "interviewer", workflow: "hire" }, { role: "recruiter", workflow: "hire" }], RECRUITER)).toEqual([
      "recruiter",
      "interviewer",
    ]);
    expect(dashboardsFor([{ role: "admin", workflow: null }], ["hire.analytics.read"])).toEqual(["talent"]);
    expect(dashboardsFor([{ role: "employee", workflow: null }], ["hire.referral.read"])).toEqual(["referrer"]);
  });

  it("leaves out a dashboard whose permissions are missing, and other workflows' roles", () => {
    expect(dashboardsFor([{ role: "recruiter", workflow: "hire" }], ["hire.candidate.read"])).toEqual([]);
    expect(dashboardsFor([{ role: "recruiter", workflow: "pay" }], RECRUITER)).toEqual([]);
    expect(dashboardsFor([{ role: "auditor", workflow: null }], RECRUITER)).toEqual([]);
  });
});

describe("links", () => {
  it("sends every record to its own page", () => {
    expect(itemHref(item("application"))).toBe(`/hire/jobs/${JOB}/pipeline?candidate=${APP}`);
    expect(itemHref(item("interview", { detail: { stage: "interview" } }))).toBe(`/hire/applications/${APP}/kit?stage=interview`);
    expect(itemHref(item("debrief"))).toBe(`/hire/jobs/${JOB}/debrief/${APP}`);
    expect(itemHref(item("shortlist"))).toBe(`/hire/jobs/${JOB}/shortlist`);
    expect(itemHref(item("offer"))).toBe(`/hire/applications/${APP}/offer`);
    expect(itemHref(item("approval", { id: "a-1" }))).toBe("/inbox/a-1");
    expect(itemHref(item("referral", { job_id: null, application_id: null }))).toBeNull();
  });

  it("links every number to the page with its records, or to the list below it", () => {
    const one = { key: "to_review", count: 3, items: [item("application")] };
    expect(sectionHref("recruiter", one)).toBe(`/hire/jobs/${JOB}/screen`);
    expect(sectionHref("recruiter", { key: "approvals", count: 1, items: [] })).toBe("/inbox");
    expect(sectionHref("interviewer", { key: "scorecards_due", count: 2, items: [] })).toBe("#dash-interviewer-scorecards-due");
  });

  it("adds up what needs the person now across dashboards", () => {
    const board = (counts: number[]): Dashboard => ({
      role: "recruiter",
      generated_at: "2026-10-14T08:00:00Z",
      timezone: "Asia/Dubai",
      scope: "mine",
      action: counts.map((count, i) => ({ key: `k${i}`, count, items: [] })),
      sections: [],
      metrics: [],
      links: [],
      survey: null,
    });
    expect(actionTotal([board([1, 2]), board([4])])).toBe(7);
  });
});
