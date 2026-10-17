// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { HireJob, HireJobDraft, HireRubricState, HireTextChecks } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));
vi.mock("@/lib/actions/jobs", () => ({
  draftJobDescription: vi.fn(),
  checkJobText: vi.fn(),
  createJob: vi.fn(),
  updateJob: vi.fn(),
  draftJobRubric: vi.fn(),
  saveJobRubric: vi.fn(),
  lockJobRubric: vi.fn(),
  publishJob: vi.fn(),
}));

const { JobEditor } = await import("../hire/job-editor");

const NO_CHECKS: HireTextChecks = { inclusive_en: [], inclusive_ar: [], sanity_en: [], sanity_ar: [] };
const TITLE = "Backend Engineer";
const DESCRIPTION = "About the role\nWe want a rockstar who builds Python services.";
const AT = TITLE.length + 1 + DESCRIPTION.indexOf("rockstar");

const DRAFT: HireJobDraft = {
  title: TITLE,
  title_ar: "مهندس خلفية",
  summary: "Build services.",
  responsibilities: [],
  requirements: [],
  nice_to_have: [],
  description_en: DESCRIPTION,
  description_ar: "عن الوظيفة",
  checks: {
    ...NO_CHECKS,
    inclusive_en: [
      {
        rule: "rockstar",
        category: "jargon",
        severity: "suggestion",
        start: AT,
        end: AT + 8,
        match: "rockstar",
        message: "Jargon can put people off.",
        suggestions: ["skilled engineer"],
      },
    ],
    sanity_en: [
      {
        rule: "years_vs_seniority",
        category: "requirements",
        severity: "warning",
        start: 0,
        end: 0,
        match: "",
        message: "Asking for 10 years of a 5 year old tool.",
        suggestions: [],
      },
    ],
  },
  model: { call_id: "00000000-0000-4000-8000-000000000001", prompt_version: "jd/v1", model: "recorded", from_recording: true },
};

const JOB: HireJob = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "backend-engineer",
  status: "draft",
  title: TITLE,
  title_ar: "",
  team: null,
  location: "Dubai",
  country: "AE",
  work_model: "hybrid",
  employment_type: "full_time",
  salary_min: "30000.00",
  salary_max: "38000.00",
  salary_currency: "AED",
  salary_period: "month",
  show_salary: true,
  description_en: DESCRIPTION,
  description_ar: "",
  rubric_policy_id: null,
  form_policy_id: null,
  created_by: null,
  owner_id: null,
  co_recruiter_ids: [],
  published_at: null,
  closed_at: null,
  created_at: "2026-10-01T08:00:00Z",
  updated_at: "2026-10-01T08:00:00Z",
};

const RUBRIC_DRAFT = {
  schema_version: 1,
  job_title: TITLE,
  must_haves: [
    { id: "python", name: "Python services", description: "Builds Python services.", evidence: "Names services they built.", weight: 8 },
  ],
  nice_to_haves: [],
  knockout_questions: [
    { id: "permit", question: "Can you work in the UAE?", question_ar: "", expected_answer: "yes", reason: "Hybrid role in Dubai." },
  ],
};

const RUBRIC: HireRubricState = {
  job_id: JOB.id,
  draft: RUBRIC_DRAFT,
  draft_source: "ai",
  draft_updated_at: "2026-10-01T08:00:00Z",
  locked: null,
  locked_version: null,
  draft_differs: true,
  versions: [],
};

const VERSION = { id: "22222222-2222-4222-8222-222222222222", version: 1, content_hash: "abc", created_at: "2026-10-01T09:00:00Z" };

function actions() {
  return {
    draftDescription: vi.fn().mockResolvedValue({ ok: true, draft: DRAFT }),
    checkText: vi.fn().mockResolvedValue({ ok: true, checks: NO_CHECKS }),
    create: vi.fn().mockResolvedValue({ ok: true, job: JOB }),
    update: vi.fn().mockResolvedValue({ ok: true, job: JOB }),
    draftRubric: vi.fn().mockResolvedValue({ ok: true, rubric: RUBRIC }),
    saveRubric: vi.fn().mockResolvedValue({ ok: true, rubric: RUBRIC }),
    lockRubric: vi.fn().mockResolvedValue({
      ok: true,
      lock: { job_id: JOB.id, version: VERSION, new_version: true, applications_on_earlier_versions: 0 },
    }),
    publish: vi.fn().mockResolvedValue({ ok: true, job: { ...JOB, status: "open", rubric_policy_id: VERSION.id } }),
  };
}

beforeEach(() => {
  routerSpies.replace.mockReset();
});

describe("JobEditor", () => {
  it("drafts from a brief, shows the checks, accepts a suggestion in one click and creates the job", async () => {
    const user = userEvent.setup();
    const a = actions();
    renderWithIntl(<JobEditor totpEnabled actions={a} />);

    const draftButton = screen.getByRole("button", { name: "Draft the description" });
    expect(draftButton).toBeDisabled();
    await user.type(screen.getByLabelText("Brief, about three lines"), "Backend engineer in Dubai, Python, hybrid.");
    await user.click(draftButton);
    expect(a.draftDescription).toHaveBeenCalledWith("Backend engineer in Dubai, Python, hybrid.");

    expect(await screen.findByDisplayValue(TITLE)).toBeInTheDocument();
    expect(screen.getByLabelText("Description in English")).toHaveValue(DESCRIPTION);
    expect(screen.getByLabelText("Description in Arabic")).toHaveValue("عن الوظيفة");
    expect(screen.getByText("Asking for 10 years of a 5 year old tool.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Use “skilled engineer”" }));
    expect(screen.getByLabelText("Description in English")).toHaveValue(
      "About the role\nWe want a skilled engineer who builds Python services.",
    );
    await waitFor(() => expect(a.checkText).toHaveBeenCalled());
    expect(await screen.findAllByText("No inclusive-language suggestions or requirement warnings.")).toHaveLength(2);

    // The rubric waits for the saved job.
    expect(screen.getByText("Create the job first. The rubric is drafted from its description.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create the job" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Write a location of at least 2 characters.");
    expect(a.create).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("Location"), "Dubai");
    await user.type(screen.getByLabelText("From"), "30000");
    await user.type(screen.getByLabelText("To"), "38000");
    await user.click(screen.getByLabelText("Show the salary on the job post"));
    await user.click(screen.getByRole("button", { name: "Create the job" }));
    await waitFor(() => expect(a.create).toHaveBeenCalled());
    expect(a.create.mock.calls[0]![0]).toMatchObject({
      title: TITLE,
      location: "Dubai",
      salary_min: "30000",
      salary_max: "38000",
      salary_currency: "AED",
      show_salary: true,
      title_ar: "مهندس خلفية",
    });
    await waitFor(() => expect(routerSpies.replace).toHaveBeenCalledWith(`/hire/jobs/${JOB.id}`));
  });

  it("says to write it yourself when no model answer is available", async () => {
    const user = userEvent.setup();
    const a = actions();
    a.draftDescription.mockResolvedValue({ ok: false, code: "model_unavailable" });
    a.draftRubric.mockResolvedValue({ ok: false, code: "model_unavailable" });
    const { unmount } = renderWithIntl(<JobEditor totpEnabled actions={a} />);
    await user.type(screen.getByLabelText("Brief, about three lines"), "A brief of enough length.");
    await user.click(screen.getByRole("button", { name: "Draft the description" }));
    expect(
      await screen.findByText("AI drafting is not available right now. Write the description yourself."),
    ).toBeInTheDocument();
    unmount();

    renderWithIntl(<JobEditor job={JOB} totpEnabled actions={a} />);
    await user.click(screen.getByRole("button", { name: "Draft the rubric" }));
    expect(await screen.findByText("AI drafting is not available right now. Write the rubric yourself.")).toBeInTheDocument();
    // An empty must-have to write by hand.
    expect(screen.getByLabelText("What evidence looks like")).toHaveValue("");
  });

  it("drafts the rubric, edits it, asks for a second factor to lock, then locks and publishes", async () => {
    const user = userEvent.setup();
    const a = actions();
    a.lockRubric.mockResolvedValueOnce({ ok: false, code: "second_factor_required" });
    renderWithIntl(<JobEditor job={JOB} totpEnabled actions={a} />);

    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Draft the rubric" }));
    expect(await screen.findByDisplayValue("Python services")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Names services they built.")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Can you work in the UAE?")).toBeInTheDocument();

    const weight = screen.getByLabelText("Weight, 1 to 10");
    await user.clear(weight);
    await user.type(weight, "9");
    await user.click(screen.getByText("Score anchors (optional)"));
    await user.type(screen.getByLabelText("Score 0"), "No Python at all.");
    await user.click(screen.getByRole("button", { name: "Lock the rubric" }));
    expect(a.saveRubric).not.toHaveBeenCalled(); // only one anchor written: refused locally
    expect(await screen.findByText(/The marked fields need a look/)).toBeInTheDocument();
    for (const n of [1, 2, 3, 4]) await user.type(screen.getByLabelText(`Score ${n}`), `Level ${n} evidence.`);

    await user.click(screen.getByRole("button", { name: "Lock the rubric" }));
    await waitFor(() => expect(a.saveRubric).toHaveBeenCalled());
    const saved = a.saveRubric.mock.calls[0]![1] as typeof RUBRIC_DRAFT & { must_haves: { weight: number; anchors: string[] }[] };
    expect(saved.must_haves[0]!.weight).toBe(9);
    expect(saved.must_haves[0]!.anchors).toEqual([
      "No Python at all.",
      "Level 1 evidence.",
      "Level 2 evidence.",
      "Level 3 evidence.",
      "Level 4 evidence.",
    ]);
    expect(await screen.findByRole("region", { name: /authenticator/i })).toBeInTheDocument();

    a.saveRubric.mockResolvedValue({ ok: true, rubric: { ...RUBRIC, draft: saved } });
    await user.click(screen.getByRole("button", { name: "Lock the rubric" }));
    expect(await screen.findByText("Rubric locked as version 1.")).toBeInTheDocument();
    expect(screen.getByText("Locked, version 1")).toBeInTheDocument();

    const publish = screen.getByRole("button", { name: "Publish" });
    await waitFor(() => expect(publish).toBeEnabled());
    await user.click(publish);
    expect(await screen.findByText("Published. The job is open on the careers page.")).toBeInTheDocument();
    expect(a.publish).toHaveBeenCalledWith(JOB.id);
  });

  it("keeps the status lines and labels in the page direction; only the Arabic fields read right to left", () => {
    const { container } = renderWithIntl(
      <JobEditor job={{ ...JOB, title_ar: "مهندس", description_ar: "وصف" }} totpEnabled actions={actions()} />,
    );
    expect(screen.getByLabelText("Description in Arabic")).toHaveAttribute("dir", "rtl");
    expect(screen.getByLabelText("Job title in Arabic")).toHaveAttribute("dir", "rtl");
    const status = container.querySelector('[data-checks-clear="ar"]');
    expect(status).not.toBeNull();
    expect(status!.closest('[dir="rtl"]')).toBeNull();
    expect(screen.getByText("Description in Arabic").closest('[dir="rtl"]')).toBeNull();
    // Nothing but the Arabic inputs carries a right-to-left direction.
    for (const el of container.querySelectorAll('[dir="rtl"]')) {
      expect(["INPUT", "TEXTAREA"]).toContain(el.tagName);
      expect(el).toHaveAttribute("lang", "ar");
    }
  });

  it("renders in Arabic, right to left, without English copy", () => {
    renderWithIntl(<JobEditor job={{ ...JOB, title_ar: "مهندس", description_ar: "وصف" }} totpEnabled actions={actions()} />, "ar");
    expect(screen.getByRole("button", { name: "اكتب مسودة الوصف" })).toBeInTheDocument();
    expect(screen.getByLabelText("الوصف بالعربية")).toHaveAttribute("dir", "rtl");
    expect(screen.getByRole("button", { name: "انشر" })).toBeDisabled();
  });
});
