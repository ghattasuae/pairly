// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { RequisitionsView } from "@/components/hire/requisitions";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Requisitions" });
  return { title: t("title") };
}

/**
 * Requisitions (brief 3.1.1): request a role, follow its approval (HR, then finance above
 * the budget threshold), and open the job once it is approved. Approvers decide in the
 * inbox with the request's details in front of them.
 */
export default async function RequisitionsPage({ params }: Props) {
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
  const retry = `/${locale}/hire/requisitions`;
  if (viewer.status !== "ready") {
    return (
      <div className="page">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.requisition.read")) notFound();
  const list = await callApi((api) => api.GET("/v1/hire/requisitions", { params: { query: { limit: 100 } } }));
  if (!list.ok) {
    return (
      <div className="page">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  const t = await getTranslations("Requisitions");
  return (
    <div className="page space-y-6">
      <PageHeader title={t("title")}>
        <p>{t("intro")}</p>
      </PageHeader>
      <RequisitionsView initial={list.data.items} canCreate={permissions.includes("hire.requisition.create")} />
    </div>
  );
}
