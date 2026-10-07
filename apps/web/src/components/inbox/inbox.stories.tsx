// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { Toaster } from "@/components/ui/toaster";
import type { ApprovalDetail, ApprovalSummary } from "@/lib/api/types";
import { DEFAULT_FILTERS } from "@/lib/inbox-filters";

import { InboxView } from "./inbox-view";

const ME = "11111111-1111-4111-8111-111111111111";
const now = Date.now();

const items: ApprovalSummary[] = [
  ["Expense note EN-0042: team lunch, AED 120", "overdue", 50],
  ["Expense note EN-0043: taxi to the airport, AED 85", "due_soon", 20],
  ["Expense note EN-0044: conference ticket, AED 1,450", "on_track", 5],
].map(([title, sla, hoursAgo], i) => ({
  id: `0000000${i + 1}-0000-4000-8000-000000000000`,
  workflow: "pay",
  approval_type: "pay.expense_note",
  title: String(title),
  status: "pending",
  run_id: "22222222-2222-4222-8222-222222222222",
  entity_type: "expense_note",
  entity_id: `EN-004${i + 2}`,
  created_at: new Date(now - Number(hoursAgo) * 3_600_000).toISOString(),
  updated_at: new Date(now).toISOString(),
  decided_at: null,
  sla_state: sla as ApprovalSummary["sla_state"],
  current_step: {
    id: `5555555${i}-0000-4000-8000-000000000000`,
    position: 0,
    name: "manager",
    mode: "any_of",
    status: "active",
    assignee_ids: [ME],
    escalation_ids: [],
    activated_at: null,
    due_at: new Date(now + (i - 1) * 3_600_000 * 6).toISOString(),
    escalated_at: null,
    completed_at: null,
  },
}));

function detail(s: ApprovalSummary): ApprovalDetail {
  return {
    ...s,
    context: { submitted_by: "Sample Person", policy: "expense.limits v3", risk: "low" },
    payload: { amount: "120.00", currency: "AED", merchant: "Sample Cafe LLC", category: "Meals" },
    payload_hash: "9f2c4e6a8b0d1f3e5a7c9e1b3d5f7a9c",
    submitted_by_type: "user",
    submitted_by_id: "someone",
    policy_version_id: "33333333-3333-4333-8333-333333333333",
    policy_version: 3,
    chain_name: "small",
    steps: s.current_step ? [s.current_step] : [],
    history: [],
    can_decide: true,
    needs_second_factor: false,
  };
}

const actions = {
  load: async (id: string) => {
    await new Promise((r) => setTimeout(r, 150));
    const s = items.find((i) => i.id === id);
    return s ? { ok: true as const, detail: detail(s) } : { ok: false as const, code: "not_found" };
  },
  decide: async () => {
    await new Promise((r) => setTimeout(r, 400));
    return { ok: false as const, code: "second_factor_required" };
  },
  loadPage: async () => ({ ok: true as const, items: [], nextCursor: null }),
};

const meta = {
  title: "Inbox/Unified inbox",
  component: InboxView,
  parameters: { layout: "fullscreen" },
  decorators: [
    (Story) => (
      <Toaster>
        <Story />
      </Toaster>
    ),
  ],
  args: {
    filters: DEFAULT_FILTERS,
    initialItems: items,
    initialCursor: null,
    initialItem: items[0]!.id,
    initialDetail: detail(items[0]!),
    workflows: ["pay", "hire"],
    meId: ME,
    totpEnabled: true,
    actions,
  },
} satisfies Meta<typeof InboxView>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
export const Empty: Story = { args: { initialItems: [], initialItem: null, initialDetail: null } };
