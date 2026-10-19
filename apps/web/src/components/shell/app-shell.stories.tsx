// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useTranslations } from "next-intl";

import { HomeSummary } from "@/components/home/home-summary";
import { ActionNeeded } from "@/components/states/action-needed";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Button } from "@/components/ui/button";
import { visibleNav } from "@/lib/nav";

import { AppShell } from "./app-shell";

/** The Home page body as it renders for Layla, every string from the messages. */
function HomePreview() {
  const home = useTranslations("Home");
  const hire = useTranslations("Hire");
  const audit = useTranslations("Audit");
  const hireTitles = useTranslations("Dashboards");
  const payTitles = useTranslations("PayDashboards");
  const sysTitles = useTranslations("SystemDashboards");
  const org = orgs[0]!.name;
  return (
    <div className="page space-y-10">
      <PageHeader
        eyebrow={org}
        title={home("greeting", { name: "Layla" })}
        actions={
          <>
            <Button variant="outline">{audit("exportCsv")}</Button>
            <Button>{hire("home.newJob")}</Button>
          </>
        }
      >
        {home("orgLine", { org })}
      </PageHeader>
      <HomeSummary
        total={5}
        now={new Date("2026-10-19T09:00:00Z")}
        tiles={[
          { anchor: "dash-recruiter", title: hireTitles("title.recruiter"), count: 3, lists: 2 },
          { anchor: "pay-dash-approver", title: payTitles("title.approver"), count: 2, lists: 1 },
          { anchor: "sys-dash-auditor", title: sysTitles("title.auditor"), count: 0, lists: 1 },
        ]}
      />
      <ActionNeeded count={0} />
    </div>
  );
}

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
    children: <HomePreview />,
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
      <div className="page">
        <NotProvisioned email="layla@acme.example" code="forbidden" />
      </div>
    ),
  },
};
export const NotProvisionedArabicDark: Story = { ...NotProvisionedState, globals: { locale: "ar", theme: "dark" } };
