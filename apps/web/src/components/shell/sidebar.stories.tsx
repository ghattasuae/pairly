// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { visibleNav } from "@/lib/nav";

import { Sidebar } from "./sidebar";

const meta = {
  title: "Shell/Sidebar",
  component: Sidebar,
  decorators: [(Story) => <div className="h-[32rem] w-60 border-e bg-sidebar"><Story /></div>],
  args: { items: visibleNav(["core.approval.read", "hire.candidate.read", "core.audit.read"]) },
} satisfies Meta<typeof Sidebar>;

export default meta;
type Story = StoryObj<typeof meta>;

export const English: Story = {};
export const Arabic: Story = { globals: { locale: "ar" } };
export const Dark: Story = { globals: { theme: "dark" } };
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
