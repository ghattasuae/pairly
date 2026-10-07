// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { StepUpPrompt } from "@/components/states/step-up-prompt";

import { JsonView } from "./json-view";
import { ApprovalStatusBadge, SlaBadge } from "./status-badges";

function Gallery() {
  return (
    <div className="max-w-3xl space-y-8 p-6">
      <section className="space-y-2">
        <h2 className="eyebrow">SLA</h2>
        <div className="flex flex-wrap gap-2">
          <SlaBadge state="on_track" />
          <SlaBadge state="due_soon" />
          <SlaBadge state="overdue" />
        </div>
      </section>
      <section className="space-y-2">
        <h2 className="eyebrow">Status</h2>
        <div className="flex flex-wrap gap-2">
          {["pending", "approved", "rejected", "changes_requested", "cancelled"].map((s) => (
            <ApprovalStatusBadge key={s} status={s} />
          ))}
        </div>
      </section>
      <section className="space-y-2">
        <h2 className="eyebrow">Data</h2>
        <div className="rounded-xl border bg-surface p-4">
          <JsonView
            value={{
              amount: "120.00",
              currency: "AED",
              merchant: "Sample Cafe LLC",
              lines: [{ item: "Lunch", qty: 2 }],
              approved_by_manager: false,
              iban: { redacted: true },
              note: null,
            }}
          />
        </div>
      </section>
      <StepUpPrompt totpEnabled returnPath="/en/inbox" onSignOut={async () => {}} />
      <StepUpPrompt totpEnabled={false} returnPath="/en/inbox" />
    </div>
  );
}

const meta = {
  title: "Data/Badges and values",
  component: Gallery,
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof Gallery>;

export default meta;
type Story = StoryObj<typeof meta>;

export const English: Story = {};
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
