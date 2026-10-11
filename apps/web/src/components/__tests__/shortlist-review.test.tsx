// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Interview } from "@/lib/hire/scheduling";
import type { ScreeningPanel } from "@/lib/hire/screening";
import type { ShortlistCandidate } from "@/lib/hire/shortlist";
import type { HireCard, HireStage } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));
vi.mock("@/lib/actions/hire", () => ({
  moveCandidate: vi.fn(),
  submitMessage: vi.fn(),
  addNote: vi.fn(),
  setTags: vi.fn(),
}));

Element.prototype.scrollIntoView = vi.fn();

const { ShortlistReview } = await import("../hire/shortlist-review");

const JOB = "99999999-9999-4999-8999-999999999999";
const STAGES: HireStage[] = [
  { key: "applied", label: "Applied", label_ar: "تقدم", kind: "active", reveal_identity: false },
  { key: "screening", label: "Screening", label_ar: "الفرز", kind: "active", reveal_identity: false },
  { key: "shortlist", label: "Shortlist", label_ar: "القائمة المختصرة", kind: "active", reveal_identity: true },
  { key: "interview", label: "Interview", label_ar: "المقابلة", kind: "active", reveal_identity: true },
  { key: "hired", label: "Hired", label_ar: "تم التوظيف", kind: "hired", reveal_identity: true },
  { key: "rejected", label: "Rejected", label_ar: "مرفوض", kind: "rejected", reveal_identity: false },
  { key: "withdrawn", label: "Withdrawn", label_ar: "انسحب", kind: "withdrawn", reveal_identity: false },
];
const id = (n: number) => `0000000${n}-0000-4000-8000-000000000000`;
const NAMES = ["Rana Haddad", "Omar Saleh", "Lina Faris"];

function card(n: number): HireCard {
  return {
    application_id: id(n),
    reference: `REF${n}-AAAA`,
    label: NAMES[n - 1]!,
    identity_hidden: false,
    stage_key: "shortlist",
    stage_entered_at: "2026-10-01T10:00:00Z",
    submitted_at: "2026-10-01T10:00:00Z",
    tags: [],
    duplicate_hints: 0,
    needs_human_review: false,
    open_message: null,
    open_message_id: null,
    referred: false,
    review_reasons: [],
    sla_overdue: false,
    waiting_days: 1,
  };
}

function screening(n: number): ScreeningPanel {
  return {
    id: `1111111${n}-0000-4000-8000-000000000000`,
    application_id: id(n),
    job_id: JOB,
    status: "decided",
    run_id: null,
    run_status: null,
    blind: false,
    blind_config: {},
    rubric_policy_id: "22222222-2222-4222-8222-222222222222",
    rubric_version: 1,
    scale: { min: 0, max: 4 },
    scored: true,
    overall_score: 70 + n,
    ai_overall_score: 70 + n,
    criteria: [
      {
        criterion_id: "python",
        name: "Python services",
        weight: 8,
        must_have: true,
        score: 3,
        model_score: 3,
        confidence: 0.9,
        evidence: [{ quote: `Built payment APIs in Python for ${n + 4} years`, start: 0, end: 10, page: null, bboxes: [] }],
        rejected_quotes: 0,
        why_not_higher: "",
        flags: [],
        overridden: false,
      },
      {
        criterion_id: "k8s",
        name: "Kubernetes",
        weight: 5,
        must_have: false,
        score: 4,
        model_score: 4,
        confidence: 0.8,
        evidence: [{ quote: "Ran Kubernetes clusters in production", start: 0, end: 10, page: null, bboxes: [] }],
        rejected_quotes: 0,
        why_not_higher: "",
        flags: [],
        overridden: false,
      },
    ],
    flags: [],
    guardrail_findings: [],
    knockouts: [],
    review_reasons: [],
    models: {},
    profile: {
      experience: [{ title: { value: "Senior Backend Engineer" }, employer: { value: "Synthetic Logistics" } }],
      skills: [{ value: "Python" }, { value: "Kubernetes" }, { value: "PostgreSQL" }],
    },
    text: null,
    document_id: null,
    approval_id: null,
    score_decision_id: null,
    decision_ids: [],
    overrides: [],
    failure_reason: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    decided_at: null,
  };
}

const INTERVIEW = {
  id: "33333333-3333-4333-8333-333333333333",
  application_id: id(1),
  job_id: JOB,
  stage_key: "shortlist",
  status: "booked",
  starts_at: "2026-10-20T09:00:00Z",
  ends_at: "2026-10-20T09:45:00Z",
  duration_min: 45,
  candidate_timezone: "Asia/Dubai",
  created_at: "2026-10-10T09:00:00Z",
  panel: [],
} as unknown as Interview;

function candidates(): ShortlistCandidate[] {
  return [1, 2, 3].map((n) => ({
    card: card(n),
    profile: null,
    screening: n === 3 ? null : screening(n),
    interviews: n === 1 ? [INTERVIEW] : [],
  }));
}

const MOVE = (n: number, to: string, draft: string | null = null) => ({
  ok: true as const,
  move: { move_id: `4444444${n}-0000-4000-8000-000000000000`, application_id: id(n), from_stage: "shortlist", to_stage: to, draft_id: draft, undo_seconds: 10 },
});

function actions() {
  return {
    moveCandidate: vi.fn(),
    submitMessage: vi.fn().mockResolvedValue({ ok: true, message: {} }),
    addNote: vi.fn().mockResolvedValue({ ok: true, noteId: "n1" }),
    setTags: vi.fn().mockResolvedValue({ ok: true, tags: ["On hold"] }),
  };
}

function render(a = actions(), locale: "en" | "ar" = "en") {
  return renderWithIntl(
    <ShortlistReview
      jobId={JOB}
      jobTitle="Backend Engineer"
      stages={STAGES}
      candidates={candidates()}
      canDecide
      canNote
      canMessage
      totpEnabled
      actions={a}
    />,
    locale,
  );
}

const article = (n: number) => document.querySelector<HTMLElement>(`[data-shortlist-candidate="${id(n)}"]`)!;

describe("ShortlistReview", () => {
  it("shows every shortlisted candidate with summary, scores, evidence and interview status", () => {
    render();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Shortlist review: Backend Engineer");
    expect(screen.getByText("3 candidates on the shortlist")).toBeInTheDocument();
    expect(screen.getByText("0 of 3 decided")).toBeInTheDocument();

    const first = within(article(1));
    expect(first.getByRole("heading", { name: "Rana Haddad" })).toBeInTheDocument();
    expect(first.getByText("71 out of 100")).toBeInTheDocument();
    expect(first.getByText("Senior Backend Engineer at Synthetic Logistics")).toBeInTheDocument();
    expect(first.getByRole("list", { name: "Skills" })).toHaveTextContent("PythonKubernetesPostgreSQL");
    expect(first.getByText("3 of 4")).toBeInTheDocument();
    // The strongest evidence first (Kubernetes scored 4).
    const quotes = first.getAllByRole("blockquote");
    expect(quotes[0]).toHaveTextContent("Ran Kubernetes clusters in production");
    expect(first.getByText(/^Booked, /)).toBeInTheDocument();

    const third = within(article(3));
    expect(third.getByText("Not scored")).toBeInTheDocument();
    expect(third.getByText("No interview yet")).toBeInTheDocument();
    expect(third.getByText("Not screened yet. Nothing here was scored by the AI.")).toBeInTheDocument();
  });

  it("advances with the keyboard: J to the candidate, A, then Enter", async () => {
    const user = userEvent.setup();
    const a = actions();
    a.moveCandidate.mockResolvedValue(MOVE(1, "interview"));
    render(a);
    await user.keyboard("j");
    expect(article(1)).toHaveFocus();
    await user.keyboard("a");
    expect(within(article(1)).getByLabelText("Advance to Interview")).toBeChecked();
    expect(within(article(1)).getByRole("button", { name: "Record decision" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(a.moveCandidate).toHaveBeenCalledTimes(1));
    expect(a.moveCandidate.mock.calls[0]![0]).toMatchObject({ applicationId: id(1), toStage: "interview" });
    expect(a.moveCandidate.mock.calls[0]![0]).not.toHaveProperty("reason");
    expect(await within(article(1)).findByText("Moved to Interview.")).toBeInTheDocument();
    expect(article(1)).toHaveAttribute("data-decided", "advance");
    expect(screen.getByText("1 of 3 decided")).toBeInTheDocument();

    // The focus stays on the decided candidate; J moves to the next one.
    await waitFor(() => expect(article(1)).toHaveFocus());
    await user.keyboard("j");
    expect(article(2)).toHaveFocus();
  });

  it("needs a reason to hold, then keeps it as a note and tags the candidate", async () => {
    const user = userEvent.setup();
    const a = actions();
    render(a);
    const second = within(article(2));
    await user.click(second.getByLabelText("Hold"));
    await user.click(second.getByRole("button", { name: "Record decision" }));
    expect(second.getByRole("alert")).toHaveTextContent("Write a reason for this decision.");
    expect(a.addNote).not.toHaveBeenCalled();

    await user.type(second.getByLabelText("Reason, required"), "Waiting for the budget sign-off");
    await user.click(second.getByRole("button", { name: "Record decision" }));
    await waitFor(() => expect(a.setTags).toHaveBeenCalledWith(id(2), ["On hold"]));
    expect(a.addNote).toHaveBeenCalledWith({
      applicationId: id(2),
      body: "On hold: Waiting for the budget sign-off",
      mentions: [],
    });
    expect(a.moveCandidate).not.toHaveBeenCalled();
    expect(await second.findByText("On hold. The reason is on the candidate's notes.")).toBeInTheDocument();
  });

  it("drafts a rejection with the reason and submits it to the approval queue", async () => {
    const user = userEvent.setup();
    const a = actions();
    const draft = "55555555-5555-4555-8555-555555555555";
    a.moveCandidate.mockResolvedValue(MOVE(3, "rejected", draft));
    render(a);
    const third = within(article(3));
    await user.click(third.getByLabelText("Draft a rejection"));
    expect(third.getByText(/nothing is sent until someone else approves it/)).toBeInTheDocument();
    await user.type(third.getByLabelText("Reason, required"), "abc");
    await user.click(third.getByRole("button", { name: "Record decision" }));
    expect(third.getByRole("alert")).toHaveTextContent("Write a reason of at least 5 characters.");

    await user.type(third.getByLabelText("Reason, required"), " no production Kubernetes{Control>}{Enter}{/Control}");
    await waitFor(() => expect(a.submitMessage).toHaveBeenCalledTimes(1));
    expect(a.moveCandidate.mock.calls[0]![0]).toMatchObject({
      applicationId: id(3),
      toStage: "rejected",
      reason: "abc no production Kubernetes",
    });
    expect(a.submitMessage.mock.calls[0]![0]).toBe(draft);
    expect(
      await third.findByText("Rejection submitted for approval. Nothing is sent until someone else approves it."),
    ).toBeInTheDocument();
  });

  it("asks for a code sign-in when the move needs a second factor", async () => {
    const user = userEvent.setup();
    const a = actions();
    a.moveCandidate.mockResolvedValue({ ok: false, code: "second_factor_required" });
    render(a);
    await user.click(within(article(1)).getByLabelText("Advance to Interview"));
    await user.click(within(article(1)).getByRole("button", { name: "Record decision" }));
    expect(await screen.findByRole("region", { name: /authenticator|second|sign in/i })).toBeInTheDocument();
    expect(article(1)).not.toHaveAttribute("data-decided");
  });

  it("renders in Arabic without English copy", () => {
    render(actions(), "ar");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("مراجعة القائمة المختصرة");
    expect(within(article(1)).getByLabelText("انقل إلى المقابلة")).toBeInTheDocument();
    expect(within(article(1)).getByRole("button", { name: "سجّل القرار" })).toBeDisabled();
    expect(screen.queryByText("Record decision")).toBeNull();
  });
});
