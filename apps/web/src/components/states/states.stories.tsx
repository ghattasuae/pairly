// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ActionNeeded } from "./action-needed";
import { NotProvisioned } from "./not-provisioned";
import { Unavailable } from "./unavailable";

const meta = {
  title: "States/ActionNeeded",
  component: ActionNeeded,
  decorators: [(Story) => <div className="max-w-3xl p-8"><Story /></div>],
  args: { count: 0 },
} satisfies Meta<typeof ActionNeeded>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Empty: Story = {};
export const EmptyArabic: Story = { globals: { locale: "ar" } };
export const EmptyDark: Story = { globals: { theme: "dark" } };
export const EmptyArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };

export const NotProvisionedEnglish: Story = {
  render: () => <NotProvisioned email="sam@example.test" code="forbidden" />,
};
export const NotProvisionedArabic: Story = { ...NotProvisionedEnglish, globals: { locale: "ar" } };
export const UnavailableEnglish: Story = { render: () => <Unavailable retryHref="#" /> };
export const UnavailableArabicDark: Story = { ...UnavailableEnglish, globals: { locale: "ar", theme: "dark" } };
