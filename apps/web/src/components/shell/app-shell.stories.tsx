// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ActionNeeded } from "@/components/states/action-needed";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { visibleNav } from "@/lib/nav";

import { AppShell } from "./app-shell";

const orgs = [
  { id: "0b9d0b6e-0000-4000-8000-000000000001", name: "Acme Trading LLC" },
  { id: "0b9d0b6e-0000-4000-8000-000000000002", name: "Falcon Logistics" },
];
const admin = ["core.org.manage", "core.users.read", "core.audit.read", "core.approval.read", "hire.candidate.read", "pay.invoice.read"];

const meta = {
  title: "Shell/AppShell",
  component: AppShell,
  args: {
    items: visibleNav(admin),
    user: { name: "Layla Haddad", email: "layla@acme.example" },
    orgs,
    activeOrgId: orgs[0]!.id,
    onSignOut: () => undefined,
    onSwitchOrg: async () => undefined,
    children: (
      <div className="mx-auto max-w-5xl space-y-10 px-8 py-10">
        <PageHeader eyebrow="Acme Trading LLC" title="Good to see you, Layla." />
        <ActionNeeded count={0} />
      </div>
    ),
  },
} satisfies Meta<typeof AppShell>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Admin: Story = {};
export const AdminArabic: Story = { globals: { locale: "ar" } };
export const AdminDark: Story = { globals: { theme: "dark" } };
export const AdminArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };

export const PayApproverOnly: Story = {
  args: { items: visibleNav(["pay.invoice.read", "pay.invoice.approve"]) },
};

export const NotProvisionedState: Story = {
  args: {
    items: visibleNav(null),
    orgs: [],
    activeOrgId: null,
    children: (
      <div className="mx-auto max-w-5xl px-8 py-10">
        <NotProvisioned email="layla@acme.example" code="forbidden" />
      </div>
    ),
  },
};
export const NotProvisionedArabicDark: Story = { ...NotProvisionedState, globals: { locale: "ar", theme: "dark" } };
