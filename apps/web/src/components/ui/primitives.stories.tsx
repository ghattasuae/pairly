// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { Badge } from "./badge";
import { Button } from "./button";
import { Input } from "./input";
import { Kbd } from "./kbd";

function Gallery() {
  return (
    <div className="space-y-6 p-8">
      <div className="flex flex-wrap gap-3">
        <Button>Approve</Button>
        <Button variant="secondary">Hold</Button>
        <Button variant="outline">Add note</Button>
        <Button variant="ghost">Skip</Button>
        <Button variant="signal">Review flag</Button>
        <Button variant="destructive">Reject</Button>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Badge>Recruiter</Badge>
        <Badge variant="signal">SLA in 2h</Badge>
        <Badge variant="outline">Draft</Badge>
        <span className="flex gap-1"><Kbd>⌘</Kbd><Kbd>K</Kbd></span>
      </div>
      <Input placeholder="you@company.com" aria-label="Email" className="max-w-sm" />
    </div>
  );
}

const meta = { title: "UI/Primitives", component: Gallery } satisfies Meta<typeof Gallery>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Light: Story = {};
export const Dark: Story = { globals: { theme: "dark" } };
export const Arabic: Story = { globals: { locale: "ar" } };
