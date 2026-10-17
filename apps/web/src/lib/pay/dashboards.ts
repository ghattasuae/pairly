// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Pay role dashboards on Home (brief 5.1 items 8 to 10): which a person gets, from
 * their roles and permissions. Pure. The API decides what each holds and checks every
 * permission again. The vendor portal home (item 11) is the portal's own page.
 */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type PayDashboard = S["PayDashboardOut"];
export type PayDashSection = S["PayDashSectionOut"];
export type PayDashItem = S["PayDashItemOut"];
export type PayDashAmount = S["PayDashAmountOut"];

export const PAY_DASHBOARDS = ["ap_clerk", "approver", "finance_controller"] as const;
export type PayDashboardKey = (typeof PAY_DASHBOARDS)[number];

export const PAY_DASHBOARD_PATHS = {
  ap_clerk: "/v1/pay/dashboards/ap-clerk",
  approver: "/v1/pay/dashboards/approver",
  finance_controller: "/v1/pay/dashboards/finance-controller",
} as const satisfies Record<PayDashboardKey, string>;

/** The permission the API checks for each one (display only). */
const NEEDS: Record<PayDashboardKey, string> = {
  ap_clerk: "pay.invoice.process",
  approver: "pay.invoice.approve",
  finance_controller: "pay.analytics.read",
};

const BY_ROLE: Record<string, PayDashboardKey> = {
  ap_clerk: "ap_clerk",
  approver: "approver",
  finance_controller: "finance_controller",
};

export interface PayRoleRef {
  role: string;
  workflow?: string | null;
}

/** The Pay dashboards for these roles (Pay or every workflow), in a stable order, and
 * only those the permissions allow. */
export function payDashboardsFor(roles: readonly PayRoleRef[], permissions: readonly string[]): PayDashboardKey[] {
  const wanted = new Set<PayDashboardKey>();
  for (const r of roles) {
    if (r.workflow && r.workflow !== "pay") continue;
    const key = BY_ROLE[r.role];
    if (key) wanted.add(key);
  }
  return PAY_DASHBOARDS.filter((d) => wanted.has(d) && permissions.includes(NEEDS[d]));
}

/** How many things need this person now, across their Pay dashboards. */
export function payActionTotal(boards: readonly PayDashboard[]): number {
  return boards.reduce((sum, b) => sum + b.action.reduce((n, s) => n + s.count, 0), 0);
}

/** Pages a dashboard links to by key. */
export const PAY_PAGE_LINKS: Record<string, string> = {
  exceptions: "/pay/exceptions",
  invoices: "/pay/invoices",
  inbox: "/inbox",
  analytics: "/pay/analytics",
  forecast: "/pay/forecast",
  month_end: "/pay/month-end",
  reconciliation: "/pay/reconciliation",
  payment_runs: "/pay/payment-runs",
};

/** Only paths inside the app are followed (the API sends app paths). */
export function safeHref(href: string | null | undefined): string | null {
  return href && href.startsWith("/") && !href.startsWith("//") ? href : null;
}
