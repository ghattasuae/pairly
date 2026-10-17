// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Dashboard } from "@/lib/hire/dashboards";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { RoleDashboard } = await import("../home/role-dashboards");

const JOB = "11111111-1111-4111-8111-111111111111";
const APP = "22222222-2222-4222-8222-222222222222";
const IV = "33333333-3333-4333-8333-333333333333";

const recruiter: Dashboard = {
  role: "recruiter",
  generated_at: "2026-10-14T08:00:00Z",
  timezone: "Asia/Dubai",
  scope: "mine",
  action: [
    {
      key: "to_review",
      count: 2,
      items: [{ kind: "application", id: APP, label: "H-7K2Q", job_id: JOB, application_id: APP, at: null, detail: { stage: "applied" }, stages: [] }],
    },
    {
      key: "waiting",
      count: 1,
      items: [
        { kind: "application", id: APP, label: "Candidate H-7K2Q", job_id: JOB, application_id: APP, at: null, detail: { waiting_days: 9, sla_overdue: true }, stages: [] },
      ],
    },
    { key: "approvals", count: 0, items: [] },
  ],
  sections: [
    {
      key: "jobs",
      count: 1,
      items: [
        {
          kind: "job",
          id: JOB,
          label: "Backend Engineer",
          job_id: JOB,
          application_id: null,
          at: null,
          detail: { status: "open", team: "Platform" },
          stages: [
            { key: "applied", label: "Applied", label_ar: "تقدم", count: 3 },
            { key: "shortlist", label: "Shortlist", label_ar: "القائمة المختصرة", count: 1 },
          ],
        },
      ],
    },
  ],
  metrics: [
    { key: "time_to_first_response", value: 1.5, unit: "days", n: 4, records: [], records_total: 0, detail: {} },
  ],
  links: ["analytics", "inbox"],
  survey: null,
};

const interviewer: Dashboard = {
  role: "interviewer",
  generated_at: "2026-10-14T08:00:00Z",
  timezone: "Asia/Dubai",
  scope: "mine",
  action: [
    {
      key: "interviews_next",
      count: 1,
      items: [
        {
          kind: "interview",
          id: IV,
          label: "Candidate H-7K2Q",
          job_id: JOB,
          application_id: APP,
          at: "2026-10-14T10:00:00Z",
          detail: { stage: "interview", meeting_url: "https://meet.pairly.example/abc" },
          stages: [],
        },
      ],
    },
  ],
  sections: [],
  metrics: [],
  links: [],
  survey: null,
};

describe("RoleDashboard", () => {
  it("opens with what needs the recruiter now, every number and record linked", () => {
    renderWithIntl(<RoleDashboard dashboard="recruiter" data={recruiter} />);
    expect(screen.getByRole("heading", { name: "Recruiting" })).toBeInTheDocument();
    expect(screen.getByText("3 things need you now")).toBeInTheDocument();
    const review = screen.getByRole("link", { name: "Applications to review: 2. Open the records" });
    expect(review).toHaveAttribute("href", `/hire/jobs/${JOB}/screen`);
    expect(screen.getByRole("link", { name: "H-7K2Q" })).toHaveAttribute("href", `/hire/jobs/${JOB}/pipeline?candidate=${APP}`);
    expect(screen.getByText("Overdue")).toBeInTheDocument();
    expect(screen.getByText(/waiting 9 days/)).toBeInTheDocument();
    expect(screen.getByText("Nothing here. You are up to date.")).toBeInTheDocument();
    const jobs = document.querySelector('[data-dash-section="jobs"]') as HTMLElement;
    expect(within(jobs).getByRole("link", { name: /Shortlist\s*1/ })).toHaveAttribute("href", `/hire/jobs/${JOB}/pipeline`);
    expect(screen.getByText("Time to first response (median)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Hiring analytics/ })).toHaveAttribute("href", "/hire/analytics");
    // More items than shown: a link to all of them.
    expect(screen.getByRole("link", { name: /See all 2/ })).toBeInTheDocument();
  });

  it("gives the interviewer the join link and the kit", () => {
    renderWithIntl(<RoleDashboard dashboard="interviewer" data={interviewer} />);
    expect(screen.getByRole("link", { name: "Join" })).toHaveAttribute("href", "https://meet.pairly.example/abc");
    expect(screen.getByRole("link", { name: "Candidate H-7K2Q" })).toHaveAttribute("href", `/hire/applications/${APP}/kit?stage=interview`);
  });

  it("reads in Arabic, with the stage names in Arabic", () => {
    renderWithIntl(<RoleDashboard dashboard="recruiter" data={recruiter} />, "ar");
    expect(screen.getByRole("heading", { name: "التوظيف" })).toBeInTheDocument();
    expect(screen.getByText("القائمة المختصرة")).toBeInTheDocument();
    expect(screen.getByText("3 أمور تحتاجك الآن")).toBeInTheDocument();
  });
});
