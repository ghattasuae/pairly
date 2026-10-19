// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ExpenseView, type ExpenseNoteRow } from "@/components/expense/expense-view";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { isNavVisible } from "@/lib/nav";
import { resolveViewer } from "@/lib/provisioning";

import { sectionMetadata } from "../section-placeholder";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return sectionMetadata(toLocale((await params).locale), "expense");
}

/** Expense notes: submit a receipt, follow your notes (`expense.note.*`, checked by the API). */
export default async function ExpensePage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="page">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  if (viewer.status !== "ready") {
    return (
      <div className="page">
        <Unavailable retryHref={`/${locale}/expense`} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!isNavVisible("expense", permissions)) notFound();
  const t = await getTranslations("Expense");
  const nav = await getTranslations("Nav");
  const notes = await callApi((api) => api.GET("/v1/expense/notes", { params: { query: { limit: 50 } } }));
  const rows: ExpenseNoteRow[] = notes.ok
    ? notes.data.items.map((n) => ({
        id: n.id,
        status: n.status,
        merchant: n.merchant,
        amount: n.amount,
        currency: n.currency,
        created_at: n.created_at,
        approval_id: n.approval_id,
      }))
    : [];
  return (
    <div className="page space-y-8">
      <PageHeader title={nav("expense")}>
        <p>{t("intro")}</p>
      </PageHeader>
      {notes.ok ? (
        <ExpenseView
          notes={rows}
          canSubmit={permissions.includes("expense.note.submit")}
          canReview={permissions.includes("expense.note.approve") || permissions.includes("core.decision.read")}
          totpEnabled={twoFactorEnabled(session)}
        />
      ) : (
        <Unavailable retryHref={`/${locale}/expense`} />
      )}
    </div>
  );
}
