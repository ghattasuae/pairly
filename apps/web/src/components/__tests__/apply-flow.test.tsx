// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEV_TURNSTILE_TOKEN, readPublicForm, type PublicNotice } from "@/lib/careers/format";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { ApplyFlow } = await import("../careers/apply-flow");

const NOTICE: PublicNotice = {
  version: "2026-10-1",
  title: "How we use AI in this application",
  points: ["An AI assistant reads your CV.", "A person makes every decision."],
  consent: "I have read how AI is used and I want to apply.",
};

const FORM = readPublicForm({
  standard: { phone: "optional", location: "hidden", profile_url: "optional" },
  questions: [{ id: "notice", kind: "text", label: "Notice period", required: true }],
  knockout_questions: [{ id: "right_to_work_uae", question: "Do you have the right to work in the UAE?", question_ar: "" }],
  files: [
    { id: "cv", label: "CV", label_ar: "السيرة الذاتية", required: true },
    { id: "portfolio", label: "Portfolio", label_ar: "", required: false },
  ],
  self_identification: [
    {
      id: "gender",
      label: "Gender",
      label_ar: "الجنس",
      options: [
        { id: "woman", label: "Woman", label_ar: "امرأة" },
        { id: "prefer_not_to_say", label: "Prefer not to say", label_ar: "أفضل عدم الإجابة" },
      ],
    },
  ],
});

type Call = { url: string; init: RequestInit };
let calls: Call[] = [];

function respond(responses: Array<{ status: number; body: unknown }>) {
  const queue = [...responses];
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      const next = queue.shift() ?? { status: 500, body: {} };
      return new Response(JSON.stringify(next.body), { status: next.status, headers: { "Content-Type": "application/json" } });
    }),
  );
}

beforeEach(() => respond([]));
afterEach(() => vi.unstubAllGlobals());

function renderFlow(locale: "en" | "ar" = "en") {
  return renderWithIntl(
    <ApplyFlow orgSlug="acme" orgName="Acme Logistics" jobSlug="backend" locale={locale} notice={NOTICE} form={FORM} />,
    locale,
  );
}

async function passNotice(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("checkbox", { name: NOTICE.consent }));
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

describe("ApplyFlow", () => {
  it("shows the AI notice first and needs consent before the form", async () => {
    const user = userEvent.setup();
    renderFlow();
    expect(screen.getByRole("heading", { name: NOTICE.title })).toBeInTheDocument();
    for (const point of NOTICE.points) expect(screen.getByText(point)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Full name/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Continue" }));
    const consent = screen.getByRole("checkbox", { name: NOTICE.consent });
    expect(consent).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Please confirm that you have read how we use AI.")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();

    await passNotice(user);
    const heading = screen.getByRole("heading", { name: "Your application" });
    expect(heading).toHaveFocus();
    expect(screen.getByRole("textbox", { name: /Full name/ })).toBeInTheDocument();
    expect(screen.getByText("Step 2 of 3:")).toBeInTheDocument();
  });

  it("works with the keyboard alone", async () => {
    const user = userEvent.setup();
    renderFlow();
    await user.tab();
    expect(screen.getByRole("checkbox", { name: NOTICE.consent })).toHaveFocus();
    await user.keyboard(" ");
    await user.tab();
    expect(screen.getByRole("button", { name: "Continue" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("heading", { name: "Your application" })).toHaveFocus();
  });

  it("explains missing fields next to them and focuses the first one", async () => {
    const user = userEvent.setup();
    renderFlow();
    await passNotice(user);
    await user.click(screen.getByRole("button", { name: "Send application" }));
    const name = screen.getByRole("textbox", { name: /Full name/ });
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveFocus();
    expect(name).toHaveAccessibleDescription("Full name is required.");
    expect(screen.getByText("CV is required.")).toBeInTheDocument();
    expect(screen.getByText("Please complete the human check.")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    // Hidden fields are not asked; optional ones say so.
    expect(screen.queryByRole("textbox", { name: /Where you live/ })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Phone \(optional\)/ })).toBeInTheDocument();
  });

  it("keeps the equal opportunity questions apart, optional and explained", async () => {
    const user = userEvent.setup();
    renderFlow();
    await passNotice(user);
    const group = screen.getByRole("group", { name: "Equal opportunity questions (optional)" });
    expect(group).toHaveAccessibleDescription(/Nobody who decides on your application sees them, and the AI never does/);
    const select = within(group).getByRole("combobox", { name: /Gender/ });
    expect(select).toHaveValue("");
    expect(within(select).getByRole("option", { name: "Skip" })).toBeInTheDocument();
    const knockouts = screen.getByRole("group", { name: "A few yes or no questions" });
    expect(knockouts).toHaveAccessibleDescription("A different answer does not reject you. A person reads it.");
  });

  it("starts, uploads every file, submits, then shows the reference", async () => {
    const user = userEvent.setup();
    respond([
      { status: 201, body: { token: "t".repeat(43), expires_at: "2026-10-08T12:00:00Z", file_fields: ["cv", "portfolio"] } },
      { status: 201, body: { field: "cv", received: true } },
      { status: 201, body: { field: "portfolio", received: true } },
      { status: 202, body: { status: "received", reference: "7KQ2-M9XD", response_days: 10 } },
    ]);
    renderFlow();
    await passNotice(user);
    await user.type(screen.getByRole("textbox", { name: /Full name/ }), "Layla Haddad");
    await user.type(screen.getByRole("textbox", { name: /Email/ }), "layla@example.test");
    await user.type(screen.getByRole("textbox", { name: /Notice period/ }), "1 month");
    await user.click(screen.getByRole("radio", { name: "Yes" }));
    await user.selectOptions(screen.getByRole("combobox", { name: /Gender/ }), "woman");
    const cv = new File(["%PDF-1.4 synthetic"], "cv.pdf", { type: "application/pdf" });
    const portfolio = new File(["%PDF-1.4 more"], "portfolio.pdf", { type: "application/pdf" });
    await user.upload(screen.getByLabelText(/^CV/), cv);
    await user.upload(screen.getByLabelText(/^Portfolio/), portfolio);
    await user.click(screen.getByRole("checkbox", { name: "I am a person (development check)" }));
    await user.click(screen.getByRole("button", { name: "Send application" }));

    expect(await screen.findByRole("heading", { name: "Application sent" })).toHaveFocus();
    expect(screen.getByText("Your reference is 7KQ2-M9XD.")).toBeInTheDocument();
    expect(screen.getByText(/within 10 days/)).toBeInTheDocument();

    expect(calls.map((c) => c.url)).toEqual([
      "/api/careers/acme/backend/apply",
      "/api/careers/acme/files",
      "/api/careers/acme/files",
      "/api/careers/acme/submit",
    ]);
    const start = JSON.parse(String(calls[0]!.init.body)) as { consent: unknown; contact: unknown; answers: unknown; knockouts: unknown; self_identification: unknown; turnstile_token: unknown };
    expect(start.consent).toEqual({ notice_version: "2026-10-1", accepted: true });
    expect(start.contact).toMatchObject({ full_name: "Layla Haddad", email: "layla@example.test", phone: null, locale: "en" });
    expect(start.answers).toEqual({ notice: "1 month" });
    expect(start.knockouts).toEqual({ right_to_work_uae: "yes" });
    expect(start.self_identification).toEqual({ gender: "woman" });
    expect(start.turnstile_token).toBe(DEV_TURNSTILE_TOKEN);
    expect((calls[0]!.init.headers as Record<string, string>)["x-pairly-apply"]).toBe("1");
    const upload = calls[1]!.init.body as FormData;
    expect(upload.get("field")).toBe("cv");
    expect(upload.get("token")).toBe("t".repeat(43));
    expect((calls[1]!.init.headers as Record<string, string>)["x-pairly-upload"]).toBe("1");
  });

  it("explains a refused application in plain words and stays on the form", async () => {
    const user = userEvent.setup();
    respond([{ status: 400, body: { code: "disposable_email" } }]);
    renderFlow();
    await passNotice(user);
    await user.type(screen.getByRole("textbox", { name: /Full name/ }), "Layla Haddad");
    await user.type(screen.getByRole("textbox", { name: /Email/ }), "x@mailinator.com");
    await user.type(screen.getByRole("textbox", { name: /Notice period/ }), "now");
    await user.click(screen.getByRole("radio", { name: "No" }));
    await user.upload(screen.getByLabelText(/^CV/), new File(["%PDF-1.4"], "cv.pdf", { type: "application/pdf" }));
    await user.click(screen.getByRole("checkbox", { name: "I am a person (development check)" }));
    await user.click(screen.getByRole("button", { name: "Send application" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Please use an email address you will keep, not a temporary one.",
    );
    expect(screen.getByRole("button", { name: "Send application" })).toBeEnabled();
    expect(calls).toHaveLength(1);
  });

  it("works in Arabic", async () => {
    const user = userEvent.setup();
    renderFlow("ar");
    await user.click(screen.getByRole("checkbox", { name: NOTICE.consent }));
    await user.click(screen.getByRole("button", { name: "متابعة" }));
    expect(screen.getByRole("heading", { name: "طلبك" })).toBeInTheDocument();
    expect(screen.getByLabelText(/^السيرة الذاتية/)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "أسئلة تكافؤ الفرص (اختيارية)" })).toBeInTheDocument();
  });
});
