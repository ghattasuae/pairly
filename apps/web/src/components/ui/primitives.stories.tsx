// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { ArrowRight, Check } from "lucide-react";

import { Badge } from "./badge";
import { Button } from "./button";
import { Input } from "./input";
import { Kbd } from "./kbd";

/**
 * The primitives in the "ink and saffron" system. One saffron primary action per view;
 * everything else is outline, ghost or ink. Cards are hairline, never shadowed.
 */
function Gallery() {
  return (
    <div className="space-y-10 p-8">
      <section className="space-y-3">
        <p className="eyebrow">Buttons</p>
        <div className="flex flex-wrap items-center gap-3">
          <Button>
            Approve <ArrowRight className="rtl:-scale-x-100" aria-hidden />
          </Button>
          <Button variant="ink">Send for approval</Button>
          <Button variant="outline">Add note</Button>
          <Button variant="secondary">Hold</Button>
          <Button variant="ghost">Skip</Button>
          <Button variant="destructive">Reject</Button>
          <Button variant="link">View the evidence</Button>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm">Small</Button>
          <Button size="lg">Large</Button>
          <Button disabled>Disabled</Button>
          <Button variant="outline" size="icon" aria-label="Done">
            <Check aria-hidden />
          </Button>
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Badges</p>
        <div className="flex flex-wrap items-center gap-2">
          <Badge>Recruiter</Badge>
          <Badge variant="outline">Draft</Badge>
          <Badge variant="signal">SLA in 2h</Badge>
          <Badge variant="info">Informational</Badge>
          <Badge variant="success">Approved</Badge>
          <Badge variant="warning">Due soon</Badge>
          <Badge variant="danger">Overdue</Badge>
          <Badge variant="ink">Final</Badge>
          <span className="flex gap-1">
            <Kbd>⌘</Kbd>
            <Kbd>K</Kbd>
          </span>
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Cards</p>
        <div className="grid max-w-3xl gap-4 sm:grid-cols-2">
          <article className="card-hairline space-y-3 p-5">
            <header className="flex items-baseline justify-between gap-2">
              <h3 className="text-sm font-medium">Invoices to code</h3>
              <span className="figure text-3xl leading-none text-signal-ink">7</span>
            </header>
            <p className="text-sm text-muted-foreground">Hairline border, no shadow. The key number is the only saffron.</p>
          </article>
          <a href="#card" className="card-hairline lift block space-y-3 p-5">
            <span className="block text-sm font-medium">A card that opens something</span>
            <span className="figure block text-3xl leading-none">AED 48,200</span>
            <span className="block font-mono text-xs text-muted-foreground">n = 42 invoices</span>
          </a>
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">Fields</p>
        <Input placeholder="you@company.com" aria-label="Email" className="max-w-sm" />
      </section>
    </div>
  );
}

const meta = { title: "UI/Primitives", component: Gallery } satisfies Meta<typeof Gallery>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Light: Story = {};
export const Dark: Story = { globals: { theme: "dark" } };
export const Arabic: Story = { globals: { locale: "ar" } };
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
