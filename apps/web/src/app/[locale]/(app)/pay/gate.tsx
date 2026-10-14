// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import type * as React from "react";

import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { Link } from "@/i18n/navigation";
import type { Locale } from "@/i18n/routing";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type PayGate =
  | { ok: false; page: React.ReactElement }
  | { ok: true; permissions: string[]; totpEnabled: boolean };

/**
 * Who may see a Pay page: signed in, provisioned, and holding `permission` (else 404,
 * so the URL reveals no more than the nav). The API checks everything again.
 */
export async function payGate(locale: Locale, permission: string, retryPath: string): Promise<PayGate> {
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return {
      ok: false,
      page: (
        <PayFrame>
          <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
        </PayFrame>
      ),
    };
  }
  if (viewer.status !== "ready") {
    return { ok: false, page: <PayUnavailable retry={`/${locale}${retryPath}`} /> };
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes(permission)) notFound();
  return { ok: true, permissions, totpEnabled: twoFactorEnabled(session) };
}

export function PayFrame({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto w-full max-w-5xl space-y-6 px-5 py-10 sm:px-8">{children}</div>;
}

export function PayUnavailable({ retry }: { retry: string }) {
  return (
    <PayFrame>
      <Unavailable retryHref={retry} />
    </PayFrame>
  );
}

/**
 * The Pay section's own tabs: vendors, invoices, exceptions, purchase orders, payment runs, bank-detail changes,
 * onboarding invites, approval rules, analytics, settings.
 */
export async function PayHeader({
  title,
  intro,
  current,
  permissions,
}: {
  title: string;
  intro?: string;
  current:
    | "vendors"
    | "invoices"
    | "exceptions"
    | "orders"
    | "runs"
    | "changes"
    | "invites"
    | "rules"
    | "settings"
    | "analytics"
    | null;
  permissions: string[];
}) {
  const t = await getTranslations("Pay.tabs");
  const tabs = [
    { key: "vendors" as const, href: "/pay", show: permissions.includes("pay.vendor.read") },
    { key: "invoices" as const, href: "/pay/invoices", show: permissions.includes("pay.invoice.read") },
    { key: "exceptions" as const, href: "/pay/exceptions", show: permissions.includes("pay.invoice.read") },
    { key: "orders" as const, href: "/pay/purchase-orders", show: permissions.includes("pay.purchase_order.read") },
    { key: "runs" as const, href: "/pay/payment-runs", show: permissions.includes("pay.payment_run.read") },
    { key: "changes" as const, href: "/pay/bank-changes", show: permissions.includes("pay.bank_change.read") },
    { key: "invites" as const, href: "/pay/invites", show: permissions.includes("pay.vendor.write") },
    { key: "rules" as const, href: "/pay/approval-rules", show: permissions.includes("pay.invoice.read") },
    { key: "analytics" as const, href: "/pay/analytics", show: permissions.includes("pay.analytics.read") },
    { key: "settings" as const, href: "/pay/settings", show: permissions.includes("pay.settings.manage") },
  ].filter((tab) => tab.show);
  return (
    <div className="space-y-4">
      <PageHeader title={title}>{intro && <p>{intro}</p>}</PageHeader>
      <nav aria-label={t("label")} className="flex flex-wrap gap-2">
        {tabs.map((tab) => (
          <Link
            key={tab.key}
            href={tab.href}
            aria-current={current === tab.key ? "page" : undefined}
            className={
              current === tab.key
                ? "inline-flex h-8 items-center rounded-md bg-secondary px-3 text-[0.8rem] font-medium"
                : "inline-flex h-8 items-center rounded-md border px-3 text-[0.8rem] hover:bg-accent"
            }
          >
            {t(tab.key)}
          </Link>
        ))}
      </nav>
    </div>
  );
}
