// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PortalApplication, PortalApplicationDetail, PortalPrivacy } from "@/lib/portal/steps";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/portal", () => ({
  requestPortalLink: vi.fn(),
  openPortalSession: vi.fn(),
  signOutOfPortal: vi.fn(),
  loadPortalApplications: vi.fn(),
  loadPortalApplication: vi.fn(),
  withdrawApplication: vi.fn(),
  replyToCompany: vi.fn(),
  loadPrivacy: vi.fn(),
  requestDeletion: vi.fn(),
  requestHumanReview: vi.fn(),
}));

const { StatusTimeline } = await import("../portal/status-timeline");
const { PortalSignIn } = await import("../portal/portal-sign-in");
const { PortalLogin } = await import("../portal/portal-login");
const { PortalHome, POLL_MS } = await import("../portal/portal-home");
const { ApplicationDetail } = await import("../portal/application-detail");
const { PrivacyCentre } = await import("../portal/privacy-centre");
const { ReceiptCriteria } = await import("../portal/receipt-criteria");

const APP_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG = "0123456789abcdef0123456789abcdef";
const TOKEN = `pl1.${ORG}.${"1".repeat(32)}.${"2".repeat(32)}.${"A".repeat(43)}`;

function app(patch: Partial<PortalApplication> = {}): PortalApplication {
  return {
    id: APP_ID,
    reference: "7KQ2-M9XD",
    job_title: "Backend Engineer",
    job_title_ar: "مهندس خلفيات",
    submitted_at: "2026-10-01T10:00:00Z",
    status: "in_review",
    status_since: "2026-10-02T10:00:00Z",
    history: [
      { step: "received", at: "2026-10-01T10:00:00Z" },
      { step: "in_review", at: "2026-10-02T10:00:00Z" },
    ],
    final: false,
    can_withdraw: true,
    open_document_requests: 1,
    interviews: [],
    ...patch,
  };
}

function detail(patch: Partial<PortalApplicationDetail> = {}): PortalApplicationDetail {
  return {
    ...app(),
    has_offer: false,
    messages: [
      {
        id: "m1",
        direction: "from_company",
        subject: "We received your application",
        text: "Hello Mona, thank you.",
        at: "2026-10-01T10:01:00Z",
      },
    ],
    document_requests: [
      {
        id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        label: "Portfolio",
        label_ar: "ملف الأعمال",
        status: "open",
        requested_at: "2026-10-02T09:00:00Z",
        received_at: null,
      },
    ],
    ...patch,
  };
}

const ME = { organisation: "Acme Logistics", org_slug: "acme", full_name: "Mona Said", email: "mona@example.test", locale: "en" };

describe("candidate portal", () => {
  beforeEach(() => {
    routerSpies.replace.mockClear();
    window.history.replaceState(null, "", "/en/portal");
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows honest steps: done, now with one sentence, and what usually comes next", () => {
    renderWithIntl(<StatusTimeline app={app()} />);
    const steps = [...document.querySelectorAll("[data-step]")].map((el) => [
      el.getAttribute("data-step"),
      el.getAttribute("data-state"),
    ]);
    expect(steps).toEqual([
      ["received", "done"],
      ["in_review", "current"],
      ["moving_forward", "ahead"],
      ["interview", "ahead"],
      ["offer", "ahead"],
    ]);
    expect(document.querySelector('[aria-current="step"]')).toHaveTextContent("Being reviewed");
    expect(screen.getByText(/a person decides/)).toBeInTheDocument();
  });

  it("asks for a magic link and says the same thing whoever asks", async () => {
    const request = vi.fn(async () => ({ ok: true as const }));
    renderWithIntl(<PortalSignIn org="acme" request={request} />);
    await userEvent.type(screen.getByLabelText("Email address"), "mona@example.test");
    await userEvent.click(screen.getByRole("button", { name: "Email me a link" }));
    expect(request).toHaveBeenCalledWith({ org: "acme", email: "mona@example.test", locale: "en" });
    expect(await screen.findByRole("status")).toHaveTextContent("If you applied with this address");
  });

  it("uses the link in the fragment once and lands on the applications", async () => {
    window.history.replaceState(null, "", `/en/portal/login#${TOKEN}`);
    const open = vi.fn(async () => ({ ok: true as const, locale: "en" }));
    renderWithIntl(<PortalLogin open={open} />);
    await waitFor(() => expect(routerSpies.replace).toHaveBeenCalledWith("/portal"));
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith(TOKEN);
    // The token never stays in the address bar.
    expect(window.location.hash).toBe("");
  });

  it("offers a new link for the same organisation when the link expired", async () => {
    window.history.replaceState(null, "", `/en/portal/login#${TOKEN}`);
    const open = vi.fn(async () => ({ ok: false as const, code: "not_found" }));
    renderWithIntl(<PortalLogin open={open} />);
    expect(await screen.findByRole("heading", { name: "This link does not work any more" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
  });

  it("updates the status by itself and says so politely", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const load = vi.fn(async () => ({
      ok: true as const,
      applications: [
        app({
          status: "moving_forward",
          history: [...app().history, { step: "moving_forward", at: "2026-10-03T10:00:00Z" }],
        }),
      ],
    }));
    renderWithIntl(<PortalHome me={ME} applications={[app()]} jobs={[]} load={load} />);
    expect(document.querySelector("[data-portal-application]")).toHaveAttribute("data-status", "in_review");
    await act(async () => {
      vi.advanceTimersByTime(POLL_MS);
    });
    await waitFor(() =>
      expect(document.querySelector("[data-portal-application]")).toHaveAttribute("data-status", "moving_forward"),
    );
    expect(document.querySelector("[data-live-status]")).toHaveTextContent("Your status changed.");
  });

  it("withdraws only after a confirmation", async () => {
    const withdraw = vi.fn(async () => ({
      ok: true as const,
      application: detail({ status: "withdrawn", final: true, can_withdraw: false }),
    }));
    const actions = { withdraw, reply: vi.fn(), upload: vi.fn() };
    renderWithIntl(<ApplicationDetail application={detail()} actions={actions as never} />);
    await userEvent.click(screen.getByRole("button", { name: "Withdraw this application" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("You cannot undo this here.");
    expect(withdraw).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Yes, withdraw" }));
    await waitFor(() => expect(withdraw).toHaveBeenCalledWith(APP_ID));
    expect(await screen.findByText(/We sent you an email to confirm/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Withdraw this application" })).toBeNull();
  });

  it("shows each criterion of a screening with its rubric meaning and the candidate's words", () => {
    renderWithIntl(
      <ReceiptCriteria
        criteria={[
          { criterion_id: "python", name: "Python", score: 3, scale_max: 4, anchor: "Solid experience.", your_evidence: ["built payment APIs in Python"] },
          { criterion_id: "aws", name: "AWS", score: 0, scale_max: 4, anchor: "No evidence in the application.", your_evidence: [] },
        ]}
        versions={{ model: "claude-x", prompts: [{ task: "hire_score", version: "v3" }] }}
      />,
    );
    const python = document.querySelector('[data-criterion="python"]') as HTMLElement;
    expect(python).toHaveTextContent("3 of 4");
    expect(python).toHaveTextContent("This score means: Solid experience.");
    expect(python).toHaveTextContent("built payment APIs in Python");
    expect(document.querySelector('[data-criterion="aws"]')).toHaveTextContent("Nothing in your application was quoted");
    expect(document.querySelector("[data-receipt-versions]")).toHaveTextContent("Model: claude-x. Prompt version: hire_score v3.");
  });

  it("shows a booked interview with its links and one waiting for a time", () => {
    const base = {
      ends_at: null,
      timezone: "Asia/Dubai",
      format: "video",
      title: "Backend Engineer",
      location: null,
      cancel_url: null,
    };
    renderWithIntl(
      <ApplicationDetail
        application={detail({
          interviews: [
            {
              ...base,
              id: "i1",
              status: "booked",
              starts_at: "2026-10-20T06:00:00Z",
              ends_at: "2026-10-20T06:45:00Z",
              duration_min: 45,
              join_url: "https://meet.example/abc",
              choose_url: null,
              reschedule_url: "https://app.example/en/schedule#hs1.x",
            },
            {
              ...base,
              id: "i2",
              status: "proposed",
              starts_at: null,
              duration_min: 30,
              format: "unspecified",
              join_url: null,
              choose_url: "https://app.example/en/schedule#hs1.y",
              reschedule_url: null,
            },
          ],
        })}
      />,
    );
    const booked = document.querySelector('[data-interview="i1"]') as HTMLElement;
    expect(booked).toHaveTextContent("Interview: Backend Engineer");
    expect(booked).toHaveTextContent("(Asia/Dubai)");
    expect(within(booked).getByRole("link", { name: "Join the meeting" })).toHaveAttribute("href", "https://meet.example/abc");
    expect(within(booked).getByRole("link", { name: "Change or cancel" })).toHaveAttribute("href", "https://app.example/en/schedule#hs1.x");
    const proposed = document.querySelector('[data-interview="i2"]') as HTMLElement;
    expect(proposed).toHaveTextContent("Choose a time that suits you (30 minutes).");
    expect(within(proposed).getByRole("link", { name: "Choose a time" })).toHaveAttribute("href", "https://app.example/en/schedule#hs1.y");
    expect(within(proposed).queryByRole("link", { name: "Join the meeting" })).toBeNull();
  });

  it("replies to the hiring team and uploads a requested file", async () => {
    const reply = vi.fn(async (_id: string, text: string) => ({
      ok: true as const,
      message: { id: "m2", direction: "from_you", subject: null, text, at: "2026-10-03T10:00:00Z" },
    }));
    const upload = vi.fn(async () => ({ ok: true as const }));
    renderWithIntl(<ApplicationDetail application={detail()} actions={{ withdraw: vi.fn(), reply, upload } as never} />);
    await userEvent.type(screen.getByLabelText("Your message to the hiring team"), "Happy to share more.");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(reply).toHaveBeenCalledWith(APP_ID, "Happy to share more."));
    expect(await screen.findByText("Happy to share more.")).toBeInTheDocument();
    const file = new File(["%PDF-1.4"], "portfolio.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Upload a file for Portfolio"), file);
    await waitFor(() => expect(upload).toHaveBeenCalledWith("dddddddd-dddd-4ddd-8ddd-dddddddddddd", file));
    expect(await screen.findByText("Received")).toBeInTheDocument();
  });

  it("asks for deletion and for a human review of one AI-assisted step", async () => {
    const privacy: PortalPrivacy = {
      decisions: [
        {
          decision_id: "dec_01JABCDEFGHJKMNPQRSTVWXYZ0",
          kind: "score",
          decided_at: "2026-10-02T10:00:00Z",
          application_id: APP_ID,
          reference: "7KQ2-M9XD",
          job_title: "Backend Engineer",
          can_appeal: true,
          appealed: false,
        },
      ],
      appeals: [],
      data_requests: [],
    };
    const deletion = vi.fn(async () => ({ ok: true as const, created: true }));
    const review = vi.fn(async () => ({
      ok: true as const,
      appeal: {
        id: "a1",
        decision_id: "dec_01JABCDEFGHJKMNPQRSTVWXYZ0",
        status: "open",
        outcome: null,
        outcome_text: null,
        created_at: "2026-10-03T10:00:00Z",
        sla_due_at: "2026-10-17T10:00:00Z",
      },
    }));
    renderWithIntl(<PrivacyCentre privacy={privacy} deletion={deletion} review={review} />);
    expect(screen.getByRole("link", { name: "See the receipt" })).toHaveAttribute(
      "href",
      "/portal/privacy/decisions/dec_01JABCDEFGHJKMNPQRSTVWXYZ0",
    );
    expect(document.querySelector("[data-export]")).toHaveAttribute("href", "/api/portal/export");
    await userEvent.click(screen.getByRole("button", { name: "Ask for a human review" }));
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Why do you want a review/), "Please have a person read my CV.");
    await userEvent.click(send);
    await waitFor(() =>
      expect(review).toHaveBeenCalledWith("dec_01JABCDEFGHJKMNPQRSTVWXYZ0", "Please have a person read my CV."),
    );
    expect(await screen.findByText("Review requested")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Send the request" }));
    await waitFor(() => expect(deletion).toHaveBeenCalledTimes(1));
    expect(await screen.findAllByText(/An admin will handle it/)).not.toHaveLength(0);
  });

  it("reads in Arabic", () => {
    renderWithIntl(<StatusTimeline app={app()} />, "ar");
    expect(document.querySelector('[aria-current="step"]')).toHaveTextContent("قيد المراجعة");
  });
});
