// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CandidateDetail, QueueItem, ScreeningPanel, ScreeningQueue } from "@/lib/hire/screening";
import type { HireMessage, HireProfile } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));
vi.mock("@/lib/actions/documents", () => ({ documentSource: vi.fn() }));
vi.mock("@/lib/actions/hire", () => ({
  loadBoard: vi.fn(),
  loadProfile: vi.fn(),
  moveCandidate: vi.fn(),
  advanceCandidate: vi.fn(),
  undoMoves: vi.fn(),
  bulkPreview: vi.fn(),
  bulkMove: vi.fn(),
  addNote: vi.fn(),
  setTags: vi.fn(),
  loadTeam: vi.fn(),
  loadTemplates: vi.fn(),
  createMessage: vi.fn(),
  loadMessage: vi.fn(),
  editMessage: vi.fn(),
  aiDraftMessage: vi.fn(),
  sendMessage: vi.fn(),
  submitMessage: vi.fn(),
  cancelMessage: vi.fn(),
  bulkMessagePreview: vi.fn(),
  bulkMessageSend: vi.fn(),
  rediscover: vi.fn(),
  referralLink: vi.fn(),
  myReferrals: vi.fn(),
}));
vi.mock("@/lib/actions/screening", () => ({
  loadQueue: vi.fn(),
  loadCandidate: vi.fn(),
  overrideScores: vi.fn(),
  setBlindMode: vi.fn(),
}));

vi.mock("@/lib/actions/scheduling", () => ({
  loadScheduling: vi.fn(async () => ({ ok: false, code: "unavailable" })),
  findSlots: vi.fn(),
  checkSlot: vi.fn(),
  createInterview: vi.fn(),
  updateInterview: vi.fn(),
  cancelInterview: vi.fn(),
  resendLink: vi.fn(),
  recordOutcome: vi.fn(),
  jobInterviews: vi.fn(),
}));

Element.prototype.scrollIntoView = vi.fn();
class NoObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", NoObserver);

const { ScreeningMode } = await import("../hire/screening-mode");
const schedulingActions = await import("@/lib/actions/scheduling");
const { Toaster } = await import("../ui/toaster");

const JOB = "99999999-9999-4999-8999-999999999999";
const STAGES = [
  { key: "applied", label: "Applied", label_ar: "تقدم", kind: "active", reveal_identity: false },
  { key: "screening", label: "Screening", label_ar: "الفرز", kind: "active", reveal_identity: false },
  { key: "shortlist", label: "Shortlist", label_ar: "القائمة المختصرة", kind: "active", reveal_identity: true },
  { key: "hired", label: "Hired", label_ar: "تم التوظيف", kind: "hired", reveal_identity: true },
  { key: "rejected", label: "Rejected", label_ar: "مرفوض", kind: "rejected", reveal_identity: false },
  { key: "withdrawn", label: "Withdrawn", label_ar: "انسحب", kind: "withdrawn", reveal_identity: false },
];

const id = (n: number) => `0000000${n}-0000-4000-8000-000000000000`;

function item(n: number, patch: Partial<QueueItem> = {}): QueueItem {
  return {
    application_id: id(n),
    reference: `REF${n}-AAAA`,
    label: `Candidate REF${n}-AAAA`,
    identity_hidden: true,
    stage_key: "applied",
    stage_kind: "active",
    tags: [],
    screening_status: "awaiting_review",
    scored: true,
    overall_score: 50 + n,
    flag_codes: [],
    needs_human_review: false,
    open_message: null,
    open_message_id: null,
    submitted_at: "2026-10-01T10:00:00Z",
    ...patch,
  };
}

const QUEUE: ScreeningQueue = {
  job_id: JOB,
  job_title: "Backend Engineer",
  blind_mode: true,
  blind_universities: true,
  can_change_blind_mode: false,
  scheduling_available: false,
  stages: STAGES,
  items: [item(1), item(2), item(3), item(4)],
};

function screening(n: number): ScreeningPanel {
  const text = `Summary\n[name] built payment APIs in Python for ${n} years at [university].`;
  const quote = "built payment APIs in Python";
  const start = text.indexOf(quote);
  return {
    id: `1111111${n}-0000-4000-8000-000000000000`,
    application_id: id(n),
    job_id: JOB,
    status: "awaiting_review",
    run_id: null,
    run_status: null,
    blind: true,
    blind_config: { enabled: true },
    rubric_policy_id: "22222222-2222-4222-8222-222222222222",
    rubric_version: 1,
    scale: { min: 0, max: 4 },
    scored: true,
    overall_score: 50 + n,
    ai_overall_score: 50 + n,
    criteria: [
      {
        criterion_id: "python",
        name: "Python",
        weight: 5,
        must_have: true,
        score: 3,
        model_score: 3,
        confidence: 0.9,
        evidence: [
          {
            quote,
            start: 0,
            end: quote.length,
            page: null,
            bboxes: [],
            display_start: start,
            display_end: start + quote.length,
          },
        ],
        rejected_quotes: 0,
        why_not_higher: "No lead role shown.",
        flags: [],
        overridden: false,
        anchor: "Solid experience.",
        next_anchor: "Leads the design of large Python systems.",
      },
      {
        criterion_id: "aws",
        name: "AWS",
        weight: 3,
        must_have: false,
        score: 0,
        model_score: 0,
        confidence: 0.6,
        evidence: [],
        rejected_quotes: 0,
        why_not_higher: "",
        flags: ["no_evidence"],
        overridden: false,
        anchor: "No evidence in the application.",
        next_anchor: "Some exposure.",
      },
    ],
    flags: [{ code: "unexplained_gap", kind: "claim", severity: "info", explanation: "gap", details: {} }],
    guardrail_findings: [],
    knockouts: [],
    review_reasons: [],
    models: {},
    profile: null,
    text,
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

function profile(n: number, patch: Partial<HireProfile> = {}): HireProfile {
  return {
    application_id: id(n),
    job_id: JOB,
    job_title: QUEUE.job_title,
    reference: `REF${n}-AAAA`,
    status: "received",
    stage_key: "applied",
    stage_label: "Applied",
    next_stage: STAGES[1] ?? null,
    submitted_at: "2026-10-01T10:00:00Z",
    blind_mode: true,
    identity_hidden: true,
    label: `Candidate REF${n}-AAAA`,
    contact: null,
    files: [],
    tags: [],
    needs_human_review: false,
    review_reasons: [],
    updates_email: true,
    talent_pool: false,
    referred_by: null,
    screening: null,
    messages: [],
    timeline: [],
    ...patch,
  };
}

const detail = (n: number): CandidateDetail => ({ screening: screening(n), profile: profile(n) });

function draft(): HireMessage {
  return {
    id: "77777777-7777-4777-8777-777777777777",
    application_id: id(1),
    template_key: "hire.candidate.rejection_screening",
    label: "Rejection after review",
    locale: "en",
    status: "draft",
    status_reason: null,
    sensitive: true,
    transactional: true,
    purpose: "rejection",
    values: {},
    fields: [],
    ai_fields: [],
    ai_available: false,
    reason_source: null,
    approval_id: null,
    candidate_opted_out: false,
    preview: { ok: true, subject: "Your application", text: "Dear Candidate REF1-AAAA,", missing: [], error: null, masked: true },
  };
}

function move(appId: string, to: string, draftId: string | null = null) {
  return {
    move_id: "88888888-8888-4888-8888-888888888888",
    application_id: appId,
    from_stage: "applied",
    to_stage: to,
    draft_id: draftId,
    undo_seconds: 60,
  };
}

function actions() {
  return {
    loadQueue: vi.fn(async () => ({ ok: true as const, queue: QUEUE })),
    loadCandidate: vi.fn(async (appId: string) => {
      const n = Number(appId[7]);
      return { ok: true as const, detail: detail(n) };
    }),
    advanceCandidate: vi.fn(async ({ applicationId }: { applicationId: string }) => ({
      ok: true as const,
      move: move(applicationId, "screening"),
    })),
    moveCandidate: vi.fn(async ({ applicationId, toStage }: { applicationId: string; toStage: string }) => ({
      ok: true as const,
      move: move(applicationId, toStage, toStage === "rejected" ? draft().id : null),
    })),
    undoMoves: vi.fn(async () => ({ ok: true as const, undo: { undone: [], cancelled_drafts: [] } })),
    setTags: vi.fn(async (_id: string, tags: string[]) => ({ ok: true as const, tags })),
    addNote: vi.fn(async () => ({ ok: true as const, noteId: "n1" })),
    loadMessage: vi.fn(async () => ({ ok: true as const, message: draft() })),
    submitMessage: vi.fn(async () => ({ ok: true as const, message: { ...draft(), status: "submitted" } })),
    editMessage: vi.fn(),
    aiDraftMessage: vi.fn(),
    sendMessage: vi.fn(),
    cancelMessage: vi.fn(),
    overrideScores: vi.fn(async () => {
      const s = screening(1);
      s.criteria[0] = { ...s.criteria[0]!, score: 4, overridden: true };
      return { ok: true as const, screening: { ...s, overall_score: 70 } };
    }),
    setBlindMode: vi.fn(),
    bulkPreview: vi.fn(async () => ({
      ok: true as const,
      summary: {
        to_stage: "screening",
        to_label: "Screening",
        total: 2,
        will_move: 2,
        already_there: [],
        not_found: [],
        from_stages: { applied: 2 },
        drafts_to_create: 0,
        messages_sent_now: 0,
        needs_approval: false,
      },
    })),
    bulkMove: vi.fn(async () => ({
      ok: true as const,
      result: { batch_id: "b", moves: [move(id(1), "screening"), move(id(2), "screening")], undo_seconds: 60 },
    })),
  };
}

function setup(a = actions(), patch: Record<string, unknown> = {}) {
  renderWithIntl(
    <Toaster>
      <ScreeningMode
        queue={QUEUE}
        initialState={{ candidate: id(1), q: "" }}
        initialDetail={detail(1)}
        canMove
        canNote
        canMessage
        canOverride
        totpEnabled
        actions={a as never}
        {...patch}
      />
    </Toaster>,
  );
  return a;
}

const CLOSE_SCHEDULING = "Close";
const label = () => document.querySelector("[data-candidate-label]")?.textContent;

beforeEach(() => {
  window.history.replaceState(null, "", `/en/hire/jobs/${JOB}/screen`);
});

describe("ScreeningMode", () => {
  it("shows the CV beside the scores, blind, and explains why not higher in one sentence", () => {
    setup();
    expect(label()).toBe("Candidate REF1-AAAA");
    const text = document.querySelector('[data-blind-text="true"]')!;
    expect(text).toHaveTextContent("built payment APIs in Python");
    expect(within(text as HTMLElement).getAllByText("[name]").length).toBe(1);
    expect(screen.getByText("Blind mode on")).toBeInTheDocument();
    expect(screen.getByText(/Hidden until the shortlist/)).toBeInTheDocument();
    const why = document.querySelector('[data-criterion="python"] [data-why]')!;
    expect(why).toHaveTextContent("Scored 3 of 4. A higher score needs this: Leads the design of large Python systems.");
    expect(document.querySelector('[data-criterion="aws"] [data-why]')).toHaveTextContent(
      "No quote in the CV supports this, so it gets no credit.",
    );
    expect(document.querySelector('[data-flag="unexplained_gap"]')).toHaveTextContent(/long gap/);
  });

  it("prefetches the next candidates and moves with J and K, keeping the position", async () => {
    const a = setup();
    // The next two (and no previous one for the first) load in the background.
    await waitFor(() => expect(a.loadCandidate).toHaveBeenCalledWith(id(2)));
    expect(a.loadCandidate).toHaveBeenCalledWith(id(3));
    const calls = a.loadCandidate.mock.calls.length;
    fireEvent.keyDown(window, { key: "j" });
    // Shown at once from the prefetch: no wait for the network.
    expect(label()).toBe("Candidate REF2-AAAA");
    expect(a.loadCandidate.mock.calls.filter(([x]) => x === id(2))).toHaveLength(1);
    await waitFor(() => expect(a.loadCandidate.mock.calls.length).toBeGreaterThan(calls));
    expect(document.querySelector("[data-position]")).toHaveTextContent("2 of 4");
    fireEvent.keyDown(window, { key: "k" });
    expect(label()).toBe("Candidate REF1-AAAA");
    expect(window.location.search).toContain(`candidate=${id(1)}`);
  });

  it("advances with A and offers an undo; the candidate stays open", async () => {
    const a = setup();
    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(a.advanceCandidate).toHaveBeenCalledTimes(1));
    expect(a.advanceCandidate.mock.calls[0]![0]).toMatchObject({ applicationId: id(1) });
    const undo = await screen.findByRole("button", { name: "Undo" });
    expect(label()).toBe("Candidate REF1-AAAA");
    await userEvent.click(undo);
    await waitFor(() => expect(a.undoMoves).toHaveBeenCalledWith(["88888888-8888-4888-8888-888888888888"]));
  });

  it("holds with H through a tag, undone by restoring the tags", async () => {
    const a = setup();
    fireEvent.keyDown(window, { key: "h" });
    await waitFor(() => expect(a.setTags).toHaveBeenCalledWith(id(1), ["On hold"]));
    await userEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(a.setTags).toHaveBeenLastCalledWith(id(1), []));
  });

  it("drafts a rejection with R into the approval queue, never sending it", async () => {
    const a = setup();
    fireEvent.keyDown(window, { key: "r" });
    await waitFor(() => expect(a.moveCandidate).toHaveBeenCalledWith(expect.objectContaining({ toStage: "rejected" })));
    const submit = await screen.findByRole("button", { name: "Submit for approval" });
    expect(screen.getByText(/Nothing is sent until another person approves it/)).toBeInTheDocument();
    expect(a.sendMessage).not.toHaveBeenCalled();
    await userEvent.click(submit);
    await waitFor(() => expect(a.submitMessage).toHaveBeenCalledTimes(1));
    expect(a.sendMessage).not.toHaveBeenCalled();
  });

  it("highlights a quote in the CV when it is selected", async () => {
    setup();
    await userEvent.click(document.querySelector<HTMLButtonElement>('[data-quote="ev-python-0"]')!);
    const mark = document.querySelector('mark[data-evidence~="ev-python-0"]')!;
    expect(mark.className).toContain("outline");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });

  it("notes with N and tags with T from the keyboard", async () => {
    const a = setup();
    fireEvent.keyDown(window, { key: "n" });
    const note = await screen.findByPlaceholderText("What should the team know?");
    expect(note).toHaveFocus();
    await userEvent.type(note, "Strong payments background");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    await waitFor(() =>
      expect(a.addNote).toHaveBeenCalledWith({ applicationId: id(1), body: "Strong payments background", mentions: [] }),
    );
    fireEvent.keyDown(window, { key: "t" });
    const tag = await screen.findByPlaceholderText("Type a tag and press Enter");
    await userEvent.type(tag, "fintech{Enter}");
    await waitFor(() => expect(a.setTags).toHaveBeenCalledWith(id(1), ["fintech"]));
  });

  it("selects with X and compares two candidates side by side on the same rubric", async () => {
    const a = setup();
    await waitFor(() => expect(a.loadCandidate).toHaveBeenCalledWith(id(2)));
    fireEvent.keyDown(window, { key: "x" });
    fireEvent.keyDown(window, { key: "j" });
    fireEvent.keyDown(window, { key: "x" });
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "c" });
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Candidate REF1-AAAA")).toBeInTheDocument();
    expect(within(dialog).getByText("Candidate REF2-AAAA")).toBeInTheDocument();
    expect(dialog.querySelector('[data-compare-row="python"]')).not.toBeNull();
    expect(dialog.querySelector('[data-compare-row="aws"]')).not.toBeNull();
  });

  it("shows a summary before a bulk move", async () => {
    const a = setup();
    fireEvent.keyDown(window, { key: "x" });
    fireEvent.keyDown(window, { key: "j" });
    fireEvent.keyDown(window, { key: "x" });
    const bar = document.querySelector<HTMLElement>("[data-bulk-bar]")!;
    await userEvent.selectOptions(within(bar).getByRole("combobox"), "screening");
    await userEvent.click(within(bar).getByRole("button", { name: "Move" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Nothing is sent to any candidate now.");
    expect(a.bulkMove).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Go ahead" }));
    await waitFor(() => expect(a.bulkMove).toHaveBeenCalledTimes(1));
  });

  it("keeps S disabled while the API says scheduling is not available", async () => {
    setup();
    expect(document.querySelector('[data-action="schedule"]')).toBeDisabled();
    expect(document.querySelector('[data-action="schedule"]')).not.toHaveAttribute("aria-describedby");
    fireEvent.keyDown(window, { key: "s" });
    await waitFor(() => expect(schedulingActions.loadScheduling).not.toHaveBeenCalled());
  });

  it("opens the scheduling panel for the current candidate with S and with the button", async () => {
    vi.mocked(schedulingActions.loadScheduling).mockClear();
    setup(actions(), { queue: { ...QUEUE, scheduling_available: true } });
    const button = document.querySelector<HTMLButtonElement>('[data-action="schedule"]')!;
    expect(button).toBeEnabled();
    fireEvent.keyDown(window, { key: "s" });
    await waitFor(() => expect(schedulingActions.loadScheduling).toHaveBeenCalledWith(id(1)));
    // The real scheduling panel (here its error state: the mocked load fails) and its
    // close button bring the scores back.
    const close = await screen.findByRole("button", { name: CLOSE_SCHEDULING });
    await userEvent.click(close);
    expect(screen.queryByRole("button", { name: CLOSE_SCHEDULING })).toBeNull();
    await userEvent.click(button);
    await waitFor(() => expect(schedulingActions.loadScheduling).toHaveBeenCalledTimes(2));
  });

  it("overrides a score with a required reason", async () => {
    const a = setup();
    const python = document.querySelector<HTMLElement>('[data-criterion="python"]')!;
    await userEvent.click(within(python).getByRole("button", { name: "Change score" }));
    await userEvent.selectOptions(within(python).getByRole("combobox"), "4");
    const save = within(python).getByRole("button", { name: "Save change" });
    expect(save).toBeDisabled();
    await userEvent.type(within(python).getByRole("textbox"), "Led the payments platform design.");
    await userEvent.click(save);
    await waitFor(() =>
      expect(a.overrideScores).toHaveBeenCalledWith({
        applicationId: id(1),
        scores: { python: 4 },
        reason: "Led the payments platform design.",
      }),
    );
    expect(await screen.findByText("Changed by a person")).toBeInTheDocument();
  });

  it("reads in Arabic, right to left labels included", () => {
    renderWithIntl(
      <Toaster>
        <ScreeningMode
          queue={QUEUE}
          initialState={{ candidate: id(1), q: "" }}
          initialDetail={detail(1)}
          canMove
          canNote
          canMessage
          canOverride={false}
          totpEnabled
          actions={actions() as never}
        />
      </Toaster>,
      "ar",
    );
    expect(screen.getByText("الوضع المحجوب مفعّل")).toBeInTheDocument();
    expect(document.querySelector('[data-criterion="python"] [data-why]')).toHaveTextContent("حصل على 3 من 4");
  });
});
