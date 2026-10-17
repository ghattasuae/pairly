// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getFormatter, getTranslations, setRequestLocale } from "next-intl/server";

import { DocumentViewer } from "@/components/document/document-viewer";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { Badge } from "@/components/ui/badge";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { highlightsFromFields, highlightsFromFindings, readContext, subjectFor, type Highlight } from "@/lib/evidence";
import { isUuid } from "@/lib/inbox-filters";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; id: string }>;
  searchParams: Promise<{ page?: string | string[]; approval?: string | string[] }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Viewer" });
  return { title: t("title") };
}

/** The newest receipt of a kind among a document's decision records, or null. */
async function latestReceipt(decisions: { id: string; kind: string }[], kind: string) {
  const found = decisions.find((d) => d.kind === kind);
  if (!found) return null;
  const result = await callApi((api) =>
    api.GET("/v1/decisions/{decision_id}/receipt", { params: { path: { decision_id: found.id } } }),
  );
  return result.ok ? (result.data as Record<string, unknown>) : null;
}

/** Guardrail findings, with the flagged text, from the approval the reviewer came from. */
async function approvalFindings(approvalId: unknown, documentId: string): Promise<Highlight[] | null> {
  if (typeof approvalId !== "string" || !isUuid(approvalId)) return null;
  const result = await callApi((api) =>
    api.GET("/v1/approvals/{approval_id}", { params: { path: { approval_id: approvalId } } }),
  );
  if (!result.ok) return null;
  const context = readContext(result.data.context);
  // Only when the approval is about this very document.
  if (context.documentId !== documentId) return null;
  return context.highlights.filter((h) => h.kind === "guardrail");
}

/**
 * One document with what the AI found in it: where each extracted value was read
 * (evidence) and any guardrail findings, drawn on the pages, with the guardrail warning
 * above them. Evidence comes from the document's newest extraction record. Findings come
 * from the approval the reviewer opened it from (`?approval=`, the inbox link; the
 * approval API decides who may read it), else from a risk record about the document
 * (the flagged text only where the receipt shows it). Without permission to read those,
 * the document shows on its own.
 */
export default async function DocumentPage({ params, searchParams }: Props) {
  const { locale: raw, id } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!isUuid(id)) notFound();
  const viewer = await resolveViewer();
  if (viewer.status !== "ready") return <Unavailable retryHref={`/${locale}/documents/${id}`} />;
  const t = await getTranslations("Viewer");
  const format = await getFormatter();
  const workflows = await getTranslations("Workflows");
  const kinds = await getTranslations("DecisionKinds");

  const [doc, decisions] = await Promise.all([
    callApi((api) => api.GET("/v1/documents/{document_id}", { params: { path: { document_id: id } } })),
    callApi((api) =>
      api.GET("/v1/decisions", { params: { query: { entity_type: "document", entity_id: id, limit: 50 } } }),
    ),
  ]);
  if (!doc.ok) {
    if (doc.code === "not_found" || doc.code === "permission_denied") notFound();
    return (
      <div className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={`/${locale}/documents/${id}`} />
      </div>
    );
  }
  const records = decisions.ok ? decisions.data : [];
  const [extractionReceipt, riskReceipt] = await Promise.all([
    latestReceipt(records, "extraction"),
    latestReceipt(records, "risk_score"),
  ]);
  const highlights: Highlight[] = [];
  const extractionId = (extractionReceipt?.inputs as { extraction_id?: unknown } | undefined)?.extraction_id;
  if (typeof extractionId === "string" && isUuid(extractionId)) {
    const extraction = await callApi((api) =>
      api.GET("/v1/extractions/{extraction_id}", { params: { path: { extraction_id: extractionId } } }),
    );
    if (extraction.ok) highlights.push(...highlightsFromFields(extraction.data.fields));
  }
  const query = await searchParams;
  const fromApproval = await approvalFindings(query.approval, id);
  highlights.push(
    ...(fromApproval ??
      highlightsFromFindings(
        (riskReceipt?.evidence as { guardrail_findings?: unknown } | undefined)?.guardrail_findings,
        (riskReceipt?.personal_data as { guardrail_evidence?: unknown } | undefined)?.guardrail_evidence,
      )),
  );

  const document = doc.data;
  const pageParam = Number(query.page);
  const workflow = workflows.has(document.workflow as "pay") ? workflows(document.workflow as "pay") : document.workflow;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 px-5 py-8 sm:px-8">
      <PageHeader eyebrow={workflow} title={<bdi>{document.original_filename ?? t("untitled")}</bdi>}>
        <div className="flex flex-wrap items-center gap-2 pt-1 text-sm">
          <Badge variant={document.needs_review ? "signal" : "outline"}>
            {document.needs_review ? t("needsReview") : t(`status.${knownStatus(document.status)}`)}
          </Badge>
          {document.page_count !== null && <span>{t("pageCount", { count: document.page_count })}</span>}
          <span>· {format.dateTime(new Date(document.created_at), { dateStyle: "medium" })}</span>
          <span className="font-mono text-xs" dir="ltr" title={document.sha256}>
            · {document.sha256.slice(0, 12)}
          </span>
        </div>
      </PageHeader>
      <DocumentViewer
        documentId={document.id}
        highlights={highlights}
        layout="split"
        subject={subjectFor(document.workflow)}
        notice
        initialPage={Number.isInteger(pageParam) && pageParam > 0 ? pageParam : 1}
      />
      {records.length > 0 && (
        <section aria-labelledby="doc-decisions" className="space-y-3">
          <h2 id="doc-decisions" className="eyebrow">
            {t("decisions")}
          </h2>
          <ul className="divide-y rounded-xl border bg-surface">
            {records.map((record) => (
              <li key={record.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                <span className="flex items-center gap-2">
                  <Badge variant="outline">{kinds.has(record.kind as "risk_score") ? kinds(record.kind as "risk_score") : record.kind}</Badge>
                  <span className="text-muted-foreground">
                    {format.dateTime(new Date(record.decided_at), { dateStyle: "medium", timeStyle: "short" })}
                  </span>
                </span>
                <Link href={`/decisions/${record.id}`} className="font-mono text-xs text-primary underline-offset-4 hover:underline">
                  {record.id}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function knownStatus(status: string) {
  return (
    (["received", "scanning", "clean", "infected", "rejected", "parsed", "parse_failed"] as const).find((s) => s === status) ??
    "other"
  );
}
