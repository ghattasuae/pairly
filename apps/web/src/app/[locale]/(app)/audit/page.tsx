// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { setRequestLocale } from "next-intl/server";

import { AuditView } from "@/components/audit/audit-view";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { auditApiQuery, auditQuery, parseAuditCursor, parseAuditFilters } from "@/lib/audit-filters";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

import { sectionMetadata } from "../section-placeholder";

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return sectionMetadata(toLocale((await params).locale), "audit");
}

/**
 * The audit viewer (brief 5.1.12 and 5.2.4) for auditors and admins (`core.audit.read`):
 * search the hash-chained log, page through it, check the chain and its anchors, export
 * what the filters select (`core.audit.export`, step-up).
 */
export default async function AuditPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session, query] = await Promise.all([resolveViewer(), getSession(), searchParams]);
  if (viewer.status !== "ready") {
    return (
      <div className="page">
        <Unavailable retryHref={`/${locale}/audit`} />
      </div>
    );
  }
  if (!viewer.me.permissions.includes("core.audit.read")) notFound();
  const filters = parseAuditFilters(query);
  const cursor = parseAuditCursor(query);
  const [events, anchors] = await Promise.all([
    callApi((api) =>
      api.GET("/v1/audit/events", {
        params: { query: { ...auditApiQuery(filters), order: filters.order, limit: 50, ...(cursor ? { cursor } : {}) } },
      }),
    ),
    callApi((api) => api.GET("/v1/audit/anchors")),
  ]);
  if (!events.ok && events.code !== "invalid_input") {
    return (
      <div className="page">
        <Unavailable retryHref={`/${locale}/audit${auditQuery(filters)}`} />
      </div>
    );
  }

  return (
    <AuditView
      filters={filters}
      events={events.ok ? events.data.items : []}
      nextCursor={events.ok ? events.data.next_cursor : null}
      paged={cursor !== null}
      invalidFilter={!events.ok}
      anchors={anchors.ok ? anchors.data : null}
      canExport={viewer.me.permissions.includes("core.audit.export")}
      totpEnabled={twoFactorEnabled(session)}
    />
  );
}
