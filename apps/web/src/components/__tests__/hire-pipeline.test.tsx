// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  HireBoard,
  HireCard,
  HireMessage,
  HireProfile,
  HireTemplate,
} from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));
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

Element.prototype.scrollIntoView = vi.fn();

const { PipelineBoard } = await import("../hire/pipeline-board");
const { ComposePanel } = await import("../hire/compose-panel");
const { Toaster } = await import("../ui/toaster");

const JOB = "99999999-9999-4999-8999-999999999999";
const STAGES = [
  { key: "applied", label: "Applied", label_ar: "تقدم", kind: "active", reveal_identity: false },
  { key: "screening", label: "Screening", label_ar: "الفرز", kind: "active", reveal_identity: false },
  { key: "shortlist", label: "Shortlist", label_ar: "القائمة المختصرة", kind: "active", reveal_identity: true },
  { key: "hired", label: "Hired", label_ar: "تم التوظيف", kind: "hired", reveal_identity: true },
  { key: "rejected", label: "Rejected", label_ar: "مرفوض", kind: "rejected", reveal_identity: false },
  { key: "withdrawn", label: "Withdrawn", label_ar: "انسحب", kind: "withdrawn", reveal_identity: false },
] satisfies HireBoard["stages"];

function card(n: number, patch: Partial<HireCard> = {}): HireCard {
  return {
    application_id: `0000000${n}-0000-4000-8000-000000000000`,
    reference: `REF${n}-AAAA`,
    label: `Person ${n}`,
    identity_hidden: false,
    stage_key: "applied",
    stage_entered_at: "2026-10-01T10:00:00Z",
    submitted_at: "2026-10-01T10:00:00Z",
    waiting_days: 2,
    sla_overdue: false,
    tags: [],
    needs_human_review: false,
    review_reasons: [],
    duplicate_hints: 0,
    referred: false,
    open_message: null,
    open_message_id: null,
    ...patch,
  };
}

const BOARD: HireBoard = {
  job_id: JOB,
  job_title: "Senior Backend Engineer",
  job_status: "open",
  blind_mode: false,
  sla_days: 7,
  stages: STAGES,
  cards: [card(1), card(2), card(3, { stage_key: "screening", sla_overdue: true, waiting_days: 9 })],
};

function profile(c: HireCard, patch: Partial<HireProfile> = {}): HireProfile {
  return {
    application_id: c.application_id,
    job_id: JOB,
    job_title: BOARD.job_title,
    reference: c.reference,
    status: "received",
    stage_key: c.stage_key,
    stage_label: "Applied",
    next_stage: STAGES[1] ?? null,
    submitted_at: c.submitted_at,
    blind_mode: false,
    identity_hidden: false,
    label: c.label,
    contact: { full_name: c.label, email: "person@example.test", phone: null, location: "Dubai", profile_url: null },
    files: [],
    tags: [],
    needs_human_review: false,
    review_reasons: [],
    updates_email: true,
    talent_pool: false,
    referred_by: null,
    screening: null,
    messages: [],
    timeline: [{ at: "2026-10-01T10:00:00Z", kind: "application", title: "Applied", detail: {} }],
    ...patch,
  };
}

const TEMPLATES: HireTemplate[] = [
  {
    key: "hire.candidate.status_update",
    label: "Status update",
    label_ar: "تحديث الحالة",
    sensitive: false,
    transactional: false,
    rejection: false,
    ai_available: true,
    suggested_for: ["applied", "screening"],
    fields: [{ name: "personal_note", type: "text", required: false, ai: true }],
  },
  {
    key: "hire.candidate.rejection_screening",
    label: "Rejection after review",
    label_ar: "اعتذار بعد المراجعة",
    sensitive: true,
    transactional: true,
    rejection: true,
    ai_available: true,
    suggested_for: ["applied"],
    fields: [{ name: "personal_note", type: "text", required: false, ai: true }],
  },
];

function message(patch: Partial<HireMessage> = {}): HireMessage {
  return {
    id: "77777777-7777-4777-8777-777777777777",
    application_id: BOARD.cards[0]!.application_id,
    template_key: "hire.candidate.status_update",
    label: "Status update",
    locale: "en",
    status: "draft",
    status_reason: null,
    sensitive: false,
    transactional: false,
    purpose: "manual",
    values: {},
    fields: [{ name: "personal_note", type: "text", required: false, ai: true }],
    ai_fields: [],
    ai_available: true,
    reason_source: null,
    approval_id: null,
    candidate_opted_out: false,
    preview: {
      ok: true,
      subject: "An update on your application: Senior Backend Engineer",
      text: "Hello Person 1,\n\nYour application is at this step: Applied.",
      missing: [],
      error: null,
      masked: false,
    },
    ...patch,
  };
}

function move(appId: string, from: string, to: string, draft: string | null = null) {
  return { move_id: "88888888-8888-4888-8888-888888888888", application_id: appId, from_stage: from, to_stage: to, draft_id: draft, undo_seconds: 60 };
}

function actions() {
  return {
    loadBoard: vi.fn(async () => ({ ok: true as const, board: BOARD })),
    loadProfile: vi.fn(async (id: string) => {
      const c = BOARD.cards.find((x) => x.application_id === id) ?? BOARD.cards[0]!;
      return { ok: true as const, profile: profile(c) };
    }),
    advanceCandidate: vi.fn(async ({ applicationId }: { applicationId: string }) => ({
      ok: true as const,
      move: move(applicationId, "applied", "screening"),
    })),
    moveCandidate: vi.fn(async ({ applicationId, toStage }: { applicationId: string; toStage: string }) => ({
      ok: true as const,
      move: move(applicationId, "applied", toStage, toStage === "rejected" ? message().id : null),
    })),
    undoMoves: vi.fn(async () => ({ ok: true as const, undo: { undone: [], cancelled_drafts: [] } })),
    loadMessage: vi.fn(async () => ({
      ok: true as const,
      message: message({ sensitive: true, template_key: "hire.candidate.rejection_screening", label: "Rejection after review", transactional: true }),
    })),
    createMessage: vi.fn(async () => ({ ok: true as const, message: message() })),
    sendMessage: vi.fn(async () => ({ ok: true as const, message: message({ status: "queued" }) })),
    submitMessage: vi.fn(async () => ({ ok: true as const, message: message({ status: "submitted", sensitive: true }) })),
    editMessage: vi.fn(async () => ({ ok: true as const, message: message() })),
    aiDraftMessage: vi.fn(),
    cancelMessage: vi.fn(),
    addNote: vi.fn(async () => ({ ok: true as const, noteId: "n1" })),
    setTags: vi.fn(async () => ({ ok: true as const, tags: [] })),
    bulkPreview: vi.fn(async () => ({
      ok: true as const,
      summary: {
        to_stage: "rejected",
        to_label: "Rejected",
        total: 2,
        will_move: 2,
        already_there: [],
        not_found: [],
        from_stages: { applied: 2 },
        drafts_to_create: 2,
        messages_sent_now: 0,
        needs_approval: true,
      },
    })),
    bulkMove: vi.fn(async () => ({
      ok: true as const,
      result: { batch_id: "b", moves: [move(BOARD.cards[0]!.application_id, "applied", "rejected")], undo_seconds: 60 },
    })),
    bulkMessagePreview: vi.fn(),
    bulkMessageSend: vi.fn(),
  };
}

function setup(a = actions(), patch: Partial<Parameters<typeof PipelineBoard>[0]> = {}) {
  renderWithIntl(
    <Toaster>
      <PipelineBoard
        board={BOARD}
        initialState={{ q: "", tag: null, flag: null, candidate: null, x: 0, y: 0 }}
        initialProfile={null}
        templates={TEMPLATES}
        team={[{ id: "12121212-1212-4212-8212-121212121212", name: "Mona Manager" }]}
        canMove
        canNote
        canMessage
        totpEnabled
        actions={a as never}
        {...patch}
      />
    </Toaster>,
  );
  return a;
}

beforeEach(() => {
  window.history.replaceState(null, "", "/en/hire/jobs/x/pipeline");
});

describe("PipelineBoard", () => {
  it("shows every stage as a column with its cards and the SLA badge", () => {
    setup();
    for (const stage of STAGES) expect(screen.getByRole("listitem", { name: stage.label })).toBeInTheDocument();
    const screening = screen.getByRole("listitem", { name: "Screening" });
    expect(within(screening).getByText("Person 3")).toBeInTheDocument();
    expect(within(screening).getByText("9 days")).toBeInTheDocument();
  });

  it("moves a candidate to the next stage in one click, with a 10 second undo", async () => {
    const a = setup();
    await userEvent.click(screen.getByRole("button", { name: "Move Person 1 to Screening" }));
    expect(a.advanceCandidate).toHaveBeenCalledTimes(1);
    expect(a.advanceCandidate.mock.calls[0]![0].applicationId).toBe(BOARD.cards[0]!.application_id);
    const undo = await screen.findByRole("button", { name: "Undo" });
    await userEvent.click(undo);
    await waitFor(() => expect(a.undoMoves).toHaveBeenCalledWith(["88888888-8888-4888-8888-888888888888"]));
  });

  it("moves the selected candidate with one key (A), and J selects the next one", async () => {
    const a = setup();
    fireEvent.keyDown(window, { key: "j" });
    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(a.advanceCandidate).toHaveBeenCalledTimes(1));
    expect(a.advanceCandidate.mock.calls[0]![0].applicationId).toBe(BOARD.cards[1]!.application_id);
  });

  it("R drafts a rejection and one click submits it to the approval queue", async () => {
    const a = setup();
    fireEvent.keyDown(window, { key: "r" });
    await waitFor(() => expect(a.moveCandidate).toHaveBeenCalledTimes(1));
    expect(a.moveCandidate.mock.calls[0]![0].toStage).toBe("rejected");
    const submit = await screen.findByRole("button", { name: /submit for approval/i });
    expect(screen.getByText(/another person approves the exact text/i)).toBeInTheDocument();
    await userEvent.click(submit);
    await waitFor(() => expect(a.submitMessage).toHaveBeenCalledTimes(1));
    expect(a.sendMessage).not.toHaveBeenCalled();
  });

  it("adds a note in two clicks: open the candidate, then Add note", async () => {
    const a = setup();
    await userEvent.click(screen.getByText("Person 2"));
    const box = await screen.findByRole("textbox", { name: "Note" });
    await userEvent.type(box, "Strong systems design.");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    await waitFor(() =>
      expect(a.addNote).toHaveBeenCalledWith({
        applicationId: BOARD.cards[1]!.application_id,
        body: "Strong systems design.",
        mentions: [],
      }),
    );
  });

  it("shows interview events on the timeline: booked, moved, cancelled, outcomes", async () => {
    const c = BOARD.cards[0]!;
    const iv = { interview_id: "i1", duration_min: 45, by_candidate: false, by: "Mona Manager", previous_starts_at: null };
    setup(actions(), {
      initialState: { q: "", tag: null, flag: null, candidate: c.application_id, x: 0, y: 0 },
      initialProfile: profile(c, {
        blind_mode: true,
        identity_hidden: true,
        timeline: [
          { at: "2026-10-05T10:00:00Z", kind: "interview", title: "Interview no show", detail: { ...iv, status: "no_show", starts_at: "2026-10-04T06:00:00Z" } },
          { at: "2026-10-04T10:00:00Z", kind: "interview", title: "Interview completed", detail: { ...iv, status: "completed", starts_at: "2026-10-04T06:00:00Z" } },
          { at: "2026-10-03T10:00:00Z", kind: "interview", title: "Interview cancelled", detail: { ...iv, status: "cancelled", by: null, by_candidate: true, starts_at: "2026-10-09T06:00:00Z" } },
          {
            at: "2026-10-02T11:00:00Z",
            kind: "interview",
            title: "Interview rescheduled",
            detail: { ...iv, status: "rescheduled", starts_at: "2026-10-09T06:00:00Z", previous_starts_at: "2026-10-08T06:00:00Z" },
          },
          { at: "2026-10-02T10:00:00Z", kind: "interview", title: "Interview booked", detail: { ...iv, status: "booked", starts_at: "2026-10-08T06:00:00Z" } },
          { at: "2026-10-01T10:00:00Z", kind: "application", title: "Applied", detail: {} },
        ],
      }),
    });
    const timeline = await waitFor(() => {
      const el = document.querySelector("[data-timeline]");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    const items = [...timeline.querySelectorAll('[data-event="interview"]')].map((el) => el.querySelector("p")?.textContent);
    expect(items).toEqual([
      "Candidate did not attend · Mona Manager",
      "Interview completed · Mona Manager",
      "Interview cancelled · by the candidate",
      "Interview moved · Mona Manager",
      "Interview booked · Mona Manager",
    ]);
    const moved = timeline.querySelectorAll('[data-event="interview"]')[3] as HTMLElement;
    expect(moved.querySelector("[data-interview-when]")?.textContent).toMatch(/^From .+ to .+$/);
    const booked = timeline.querySelectorAll('[data-event="interview"]')[4] as HTMLElement;
    expect(booked.querySelector("[data-interview-when]")?.textContent).toMatch(/^For /);
  });

  it("sends a templated update in three clicks with the exact preview", async () => {
    const a = setup();
    await userEvent.click(screen.getByText("Person 1"));
    await userEvent.click(await screen.findByRole("button", { name: "Status update" }));
    const preview = await screen.findByText(/your application is at this step/i);
    expect(preview).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(a.sendMessage).toHaveBeenCalledTimes(1));
    expect(a.createMessage).toHaveBeenCalledWith({
      applicationId: BOARD.cards[0]!.application_id,
      templateKey: "hire.candidate.status_update",
    });
  });

  it("shows a summary before a bulk rejection and sends nothing", async () => {
    const a = setup();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Person 1" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Person 2" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Move to stage" }), "rejected");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/2 rejection drafts will be created/i)).toBeInTheDocument();
    expect(within(dialog).getByText("Nothing is sent to any candidate now.")).toBeInTheDocument();
    expect(a.bulkMove).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Move them" }));
    await waitFor(() => expect(a.bulkMove).toHaveBeenCalledTimes(1));
  });

  it("keeps the open candidate and the filters in the URL", async () => {
    setup();
    await userEvent.type(screen.getByRole("textbox", { name: /search name/i }), "person");
    await userEvent.click(screen.getByText("Person 2"));
    await waitFor(() => expect(window.location.search).toContain(`candidate=${BOARD.cards[1]!.application_id}`));
    expect(window.location.search).toContain("q=person");
  });

  it("asks for a second factor when the API needs one", async () => {
    const a = actions();
    a.advanceCandidate.mockResolvedValueOnce({ ok: false, code: "second_factor_required" } as never);
    setup(a);
    await userEvent.click(screen.getByRole("button", { name: "Move Person 1 to Screening" }));
    expect(await screen.findByRole("region", { name: /authenticator code/i })).toBeInTheDocument();
  });
});

describe("ComposePanel", () => {
  it("marks sensitive messages and fills in the AI paragraph", async () => {
    const ai = vi.fn(async () => ({
      ok: true as const,
      draft: {
        message: message({
          sensitive: true,
          values: { personal_note: "Thank you for your patience." },
          ai_fields: ["personal_note"],
        }),
        model: "m",
        prompt_version: "hire_message_draft/v1",
        from_recording: true,
      },
    }));
    renderWithIntl(
      <ComposePanel
        message={message({ sensitive: true })}
        onBack={vi.fn()}
        onDone={vi.fn()}
        onError={vi.fn()}
        actions={{ edit: vi.fn(), ai, send: vi.fn(), submit: vi.fn(), cancel: vi.fn() } as never}
      />,
    );
    expect(screen.getByText("Needs approval")).toBeInTheDocument();
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Write with AI" }));
    });
    expect(ai).toHaveBeenCalledWith(message().id, false);
    expect(await screen.findByDisplayValue("Thank you for your patience.")).toBeInTheDocument();
    expect(screen.getByText("AI draft")).toBeInTheDocument();
  });
});
