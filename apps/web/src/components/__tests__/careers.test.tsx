// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PublicJob } from "@/lib/careers/format";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { JobDescription, JobFacts, JobList } = await import("../careers/job-parts");

const JOB: PublicJob = {
  slug: "senior-backend-engineer",
  title: "Senior Backend Engineer",
  title_ar: "مهندس/ة خلفيات أول",
  team: "Delivery planning",
  location: "Dubai",
  country: "AE",
  work_model: "hybrid",
  employment_type: "full_time",
  salary: { min: "30000.00", max: "38000.00", currency: "AED", period: "month" },
  published_at: "2026-10-01T09:00:00+00:00",
  url: "https://jobs.example/en/careers/acme/senior-backend-engineer",
};

describe("JobList", () => {
  it("lists open jobs as links with their facts", () => {
    renderWithIntl(<JobList orgSlug="acme" jobs={[JOB, { ...JOB, slug: "ap", title: "AP Specialist", salary: null }]} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    const link = screen.getByRole("link", { name: "Senior Backend Engineer" });
    expect(link).toHaveAttribute("href", "/careers/acme/senior-backend-engineer");
    expect(within(items[0]!).getByText("AED 30,000 to 38,000 a month")).toBeInTheDocument();
    expect(within(items[0]!).getByText(/Hybrid/)).toBeInTheDocument();
    expect(within(items[1]!).getByText("Salary not published")).toBeInTheDocument();
    // Facts are a description list with readable labels for screen readers.
    expect(within(items[0]!).getByText("Pay")).toHaveClass("sr-only");
  });

  it("says so when there are no open jobs", () => {
    renderWithIntl(<JobList orgSlug="acme" jobs={[]} />);
    expect(screen.getByText("There are no open roles right now. Please check again soon.")).toBeInTheDocument();
  });

  it("shows Arabic titles and labels in Arabic", () => {
    renderWithIntl(<JobList orgSlug="acme" jobs={[JOB]} />, "ar");
    expect(screen.getByRole("link", { name: "مهندس/ة خلفيات أول" })).toBeInTheDocument();
    expect(screen.getByText(/مختلط/)).toBeInTheDocument();
    expect(screen.getByText(/شهريا/)).toBeInTheDocument();
  });
});

describe("JobFacts", () => {
  it("leaves out the team when there is none", () => {
    renderWithIntl(<JobFacts job={{ ...JOB, team: null, work_model: "remote" }} />);
    expect(screen.queryByText("Team")).not.toBeInTheDocument();
    expect(screen.getByText(/Remote/)).toBeInTheDocument();
  });
});

describe("JobDescription", () => {
  it("renders sections as headings and lists as lists", () => {
    renderWithIntl(<JobDescription text={"About the role\nYou will build.\n\nWhat you need\n- Python\n- SQL"} />);
    expect(screen.getByRole("heading", { level: 2, name: "About the role" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "What you need" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Python", "SQL"]);
  });

  it("never renders markup from the job text", () => {
    const { container } = renderWithIntl(<JobDescription text={"<img src=x onerror=alert(1)> hello."} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)> hello.")).toBeInTheDocument();
  });
});
