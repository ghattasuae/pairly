// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { AlertTriangle, Check, CheckCircle2, Hourglass, Info, Lock } from "lucide-react";

import { chipClass } from "./chip";
import { Callout, ConfidenceMeter, Figure, StepList } from "./status";

/**
 * Status patterns: callouts in each tone, labelled big numbers, the confidence meter,
 * the step list and filter chips. Tones are semantic; saffron stays for actions.
 */
function StatusPatterns() {
  return (
    <div className="space-y-10 p-8">
      <section className="space-y-3">
        <p className="eyebrow">Callouts</p>
        <div className="grid gap-3 md:grid-cols-2">
          <Callout tone="danger" icon={<Lock aria-hidden />} title="Payments to this vendor are locked">
            <p className="text-danger-ink">Nothing is paid until the number on file is called and two people approve.</p>
          </Callout>
          <Callout tone="warning" icon={<Hourglass aria-hidden />} title="Approved, cooling off">
            <p className="text-warning-ink">The new details are not used for 24 hours.</p>
          </Callout>
          <Callout tone="success" icon={<CheckCircle2 aria-hidden />} title="The new details are active" />
          <Callout tone="info" icon={<Info aria-hidden />} title="Sent for approval">
            <p>Another person approves the message before it goes out.</p>
          </Callout>
          <Callout tone="neutral" icon={<AlertTriangle aria-hidden />} title="Nothing to review" />
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Figures</p>
        <div className="grid gap-6 sm:grid-cols-3">
          <Figure label="Run total" value="AED 28,188.43" hint="2 invoices" />
          <Figure label="Debits not matched" value="1" tone="danger" />
          <Figure label="Lines matched" value="42" tone="success" />
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Confidence</p>
        <div className="flex flex-col gap-2 text-xs text-muted-foreground">
          <ConfidenceMeter value={0.97} tone="success">Confidence 97%</ConfidenceMeter>
          <ConfidenceMeter value={0.84} tone="neutral">Confidence 84%</ConfidenceMeter>
          <ConfidenceMeter value={0.41} tone="warning">Confidence 41%</ConfidenceMeter>
          <ConfidenceMeter value={0} tone="danger">Confidence 0%</ConfidenceMeter>
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Steps</p>
        <StepList
          label="Where the change stands"
          steps={[
            { key: "requested", state: "done", title: "Requested", body: "Oct 14, 2026", icon: <Check /> },
            { key: "call", state: "done", title: "Call back", body: "Confirmed by the vendor", icon: <Check /> },
            { key: "approvals", state: "current", title: "Two approvals", body: "Waiting for the second approver" },
            { key: "blocked", state: "blocked", title: "Second approver unavailable" },
            { key: "active", state: "todo", title: "Active" },
          ]}
        />
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Chips</p>
        <nav aria-label="Status" className="flex flex-wrap gap-2">
          <a href="#all" aria-current="page" className={chipClass(true)}>
            All <span className="tabular-nums text-muted-foreground">12</span>
          </a>
          <a href="#review" className={chipClass(false)}>
            Needs review <span className="tabular-nums text-muted-foreground">3</span>
          </a>
        </nav>
      </section>
    </div>
  );
}

const meta = { title: "UI/Status", component: StatusPatterns } satisfies Meta<typeof StatusPatterns>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Light: Story = {};
export const Dark: Story = { globals: { theme: "dark" } };
export const Arabic: Story = { globals: { locale: "ar" } };
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
