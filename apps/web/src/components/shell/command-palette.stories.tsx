// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { visibleNav } from "@/lib/nav";

import { CommandPalette } from "./command-palette";

const meta = {
  title: "Shell/CommandPalette",
  component: CommandPalette,
  args: {
    open: true,
    onOpenChange: () => undefined,
    onSignOut: () => undefined,
    items: visibleNav(["core.approval.read", "pay.invoice.read", "core.audit.read"]),
  },
} satisfies Meta<typeof CommandPalette>;

export default meta;
type Story = StoryObj<typeof meta>;

export const English: Story = {};
export const Arabic: Story = { globals: { locale: "ar" } };
export const Dark: Story = { globals: { theme: "dark" } };
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
