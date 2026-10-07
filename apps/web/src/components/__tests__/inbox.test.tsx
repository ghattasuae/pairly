// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalDetail, ApprovalSummary } from "@/lib/api/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));
vi.mock("@/lib/actions/approvals", () => ({
  loadApproval: vi.fn(),
  decideApproval: vi.fn(),
  loadInboxPage: vi.fn(),
  describeApprovalLink: vi.fn(),
  confirmApprovalLink: vi.fn(),
}));
vi.mock("@/lib/actions/documents", () => ({ documentSource: vi.fn() }));

// jsdom has no layout: scrolling is a no-op.
Element.prototype.scrollIntoView = vi.fn();

const { InboxView } = await import("../inbox/inbox-view");
const { ApprovalLinkLanding } = await import("../approvals/approval-link-landing");
const { DEFAULT_FILTERS } = await import("@/lib/inbox-filters");

const ME = "11111111-1111-4111-8111-111111111111";

function summary(n: number): ApprovalSummary {
  return {
    id: `0000000${n}-0000-4000-8000-000000000000`,
    workflow: "pay",
    approval_type: "pay.invoice",
    title: `Invoice ${n}`,
    status: "pending",
    run_id: "22222222-2222-4222-8222-222222222222",
    entity_type: null,
    entity_id: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    decided_at: null,
    sla_state: n === 1 ? "overdue" : "on_track",
    current_step: null,
  };
}

function detail(s: ApprovalSummary, patch: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return {
    ...s,
    context: {},
    payload: { amount: "120.00", currency: "AED" },
    payload_hash: "abc",
    submitted_by_type: "user",
    submitted_by_id: "someone",
    policy_version_id: "33333333-3333-4333-8333-333333333333",
    policy_version: 1,
    chain_name: "small",
    steps: [],
    history: [],
    can_decide: true,
    needs_second_factor: false,
    ...patch,
  };
}

const items = [summary(1), summary(2), summary(3)];

function setup(decide = vi.fn()) {
  const load = vi.fn(async (id: string) => {
    const s = items.find((i) => i.id === id)!;
    return { ok: true as const, detail: detail(s) };
  });
  const loadPage = vi.fn();
  renderWithIntl(
    <InboxView
      filters={DEFAULT_FILTERS}
      initialItems={items}
      initialCursor={null}
      initialItem={null}
      initialDetail={null}
      workflows={["pay"]}
      meId={ME}
      totpEnabled
      actions={{ load, decide, loadPage }}
    />,
  );
  return { load, decide };
}

describe("InboxView", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/en/inbox");
  });

  it("lists items with SLA badges and moves with J and K", async () => {
    const { load } = setup();
    expect(screen.getByText("Invoice 1", { selector: "a bdi" })).toBeInTheDocument();
    expect(screen.getAllByText("Overdue").some((el) => el.tagName === "SPAN")).toBe(true);
    const row = (n: number) => screen.getByText(`Invoice ${n}`, { selector: "a bdi" }).closest("a")!;
    expect(row(1)).toHaveAttribute("aria-current", "true");
    await waitFor(() => expect(load).toHaveBeenCalledWith(items[0]!.id));

    fireEvent.keyDown(window, { key: "j" });
    expect(row(2)).toHaveAttribute("aria-current", "true");
    fireEvent.keyDown(window, { key: "k" });
    expect(row(1)).toHaveAttribute("aria-current", "true");
    // The open item is in the URL.
    await waitFor(() => expect(window.location.search).toContain(`item=${items[0]!.id}`));
  });

  it("approves after a confirmation, optimistically, with an idempotency key", async () => {
    let resolve!: (v: unknown) => void;
    const decide = vi.fn((_input: unknown) => new Promise((r) => (resolve = r)));
    setup(decide);
    await screen.findByRole("button", { name: /^Approve/ });

    fireEvent.keyDown(window, { key: "a" });
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Invoice 1");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(decide).toHaveBeenCalledTimes(1);
    const input = decide.mock.calls[0]![0] as { approvalId: string; decision: string; idempotencyKey: string };
    expect(input).toMatchObject({ approvalId: items[0]!.id, decision: "approve" });
    expect(input.idempotencyKey).toMatch(/^web:/);
    // Selection moved on at once; the item shows as being decided.
    const row = (n: number) => screen.getByText(`Invoice ${n}`, { selector: "a bdi" }).closest("a")!;
    expect(row(2)).toHaveAttribute("aria-current", "true");

    await act(async () =>
      resolve({
        ok: true,
        result: { approval_id: items[0]!.id, approval_status: "approved", decision: "approve", decision_id: "d", replayed: false, step: "s" },
      }),
    );
    expect(await screen.findAllByText("Approved")).not.toHaveLength(0);
  });

  it("needs a reason to reject", async () => {
    const decide = vi.fn(async () => ({ ok: false as const, code: "unavailable" }));
    setup(decide);
    await screen.findByRole("button", { name: /^Reject/ });
    fireEvent.keyDown(window, { key: "r" });
    await screen.findByRole("dialog");
    const confirm = screen.getByRole("button", { name: "Reject" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason"), "Wrong amount");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ decision: "reject", reason: "Wrong amount" }));
  });

  it("rolls back and asks for the code when the API wants a second factor", async () => {
    const decide = vi.fn(async () => ({ ok: false as const, code: "second_factor_required" }));
    setup(decide);
    await screen.findByRole("button", { name: /^Approve/ });
    fireEvent.keyDown(window, { key: "a" });
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("button", { name: "Sign in again with my code" })).toBeInTheDocument();
    const row = screen.getByText("Invoice 1", { selector: "a bdi" }).closest("a")!;
    expect(row).toHaveAttribute("aria-current", "true");
  });

  it("opens the shortcut help with ?", async () => {
    setup();
    fireEvent.keyDown(window, { key: "?" });
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  });
});

describe("ApprovalLinkLanding", () => {
  const TOKEN = "T".repeat(43);
  const link = {
    approval_id: items[0]!.id,
    title: "Invoice 1",
    workflow: "pay",
    approval_type: "pay.invoice",
    step: "manager",
    action: "approve",
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    state: "valid",
    second_factor_required: true,
    has_second_factor: true,
  };

  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState(null, "", `/en/approvals/link#${TOKEN}`);
  });

  it("removes the token from the address bar and describes the link", async () => {
    const describe = vi.fn(async () => ({ kind: "ready" as const, link, email: "a@b.test" }));
    const confirm = vi.fn(async () => ({
      ok: true as const,
      result: { approval_id: link.approval_id, approval_status: "approved", decision: "approve", decision_id: "d", replayed: false, step: "manager" },
    }));
    renderWithIntl(<ApprovalLinkLanding sso={{ google: false, microsoft: false }} totpEnabled describe={describe} confirm={confirm} />);
    expect(await screen.findByRole("heading", { name: "Approve this item" })).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(describe).toHaveBeenCalledWith(TOKEN);
    expect(confirm).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Approve now" }));
    expect(confirm).toHaveBeenCalledWith(TOKEN, null);
    expect(await screen.findByText(/You approved/)).toBeInTheDocument();
  });

  it("looks the same for expired, used and someone else's links", async () => {
    const describe = vi.fn(async () => ({ kind: "invalid" as const, email: "a@b.test" }));
    renderWithIntl(<ApprovalLinkLanding sso={{ google: false, microsoft: false }} totpEnabled describe={describe} />);
    expect(await screen.findByRole("heading", { name: "This link cannot be used" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("asks a signed-out person to sign in first", async () => {
    const describe = vi.fn(async () => ({ kind: "signedOut" as const }));
    renderWithIntl(<ApprovalLinkLanding sso={{ google: false, microsoft: false }} totpEnabled describe={describe} />);
    expect(await screen.findByRole("heading", { name: "Sign in to continue" })).toBeInTheDocument();
    expect(screen.getByLabelText("Work email")).toBeInTheDocument();
  });

  it("asks for the authenticator code when the session has none", async () => {
    const describe = vi.fn(async () => ({ kind: "ready" as const, link: { ...link, has_second_factor: false }, email: "a@b.test" }));
    renderWithIntl(<ApprovalLinkLanding sso={{ google: false, microsoft: false }} totpEnabled describe={describe} />);
    expect(await screen.findByRole("button", { name: "Sign in again with my code" })).toBeInTheDocument();
  });
});
