// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { InboxView } from "@/components/inbox/inbox-view";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { apiQuery, approvalWorkflows, parseFilters, parseItem, toQuery } from "@/lib/inbox-filters";
import { isNavVisible } from "@/lib/nav";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Nav" });
  return { title: t("inbox") };
}

/**
 * The unified approval inbox. The first page of items and the open item are rendered on
 * the server (fast first paint); the rest happens in place (components/inbox).
 */
export default async function InboxPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session, query] = await Promise.all([resolveViewer(), getSession(), searchParams]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={`/${locale}/inbox`} />
      </div>
    );
  }
  if (!isNavVisible("inbox", viewer.me.permissions)) notFound();

  const filters = parseFilters(query);
  const item = parseItem(query);
  const [list, detail] = await Promise.all([
    callApi((api) => api.GET("/v1/approvals", { params: { query: apiQuery(filters, null) } })),
    item
      ? callApi((api) => api.GET("/v1/approvals/{approval_id}", { params: { path: { approval_id: item } } }))
      : Promise.resolve(null),
  ]);
  if (!list.ok) {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={`/${locale}/inbox${toQuery(filters, item)}`} />
      </div>
    );
  }

  return (
    <InboxView
      // A new filter set starts a fresh list (and keeps nothing stale).
      key={toQuery({ ...filters, q: "" }, null)}
      filters={filters}
      initialItems={list.data.items}
      initialCursor={list.data.next_cursor}
      initialItem={item}
      initialDetail={detail?.ok ? detail.data : null}
      workflows={approvalWorkflows(viewer.me.permissions)}
      meId={viewer.me.id}
      totpEnabled={twoFactorEnabled(session)}
    />
  );
}
