// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowLeft, Check, ExternalLink, EyeOff, FileText, Loader2, RotateCcw, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";
import type * as React from "react";

import { JsonView } from "@/components/data/json-view";
import { ApprovalStatusBadge, SlaBadge } from "@/components/data/status-badges";
import { DocumentViewer } from "@/components/document/document-viewer";
import { GuardrailNotice } from "@/components/document/guardrail-notice";
import { isPayContext, PayApprovalEvidence } from "@/components/pay/approval-evidence";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { Link } from "@/i18n/navigation";
import type { ApprovalDetail, ApprovalSummary, DecisionChoice } from "@/lib/api/types";
import { guardrailHighlights, readContext, subjectFor } from "@/lib/evidence";
import { cn } from "@/lib/utils";

export type DetailState =
  | { status: "loading" }
  | { status: "error"; code: string }
  | { status: "ready"; detail: ApprovalDetail };

export interface Optimistic {
  decision: DecisionChoice;
  state: "sending" | "done";
}

function Section({ title, children, id }: { title: string; children: React.ReactNode; id: string }) {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h3 id={id} className="eyebrow">
        {title}
      </h3>
      {children}
    </section>
  );
}

/** Words and workflow names stay readable whatever a workflow calls them. */
function useLabels() {
  const workflows = useTranslations("Workflows");
  return {
    workflow: (id: string) => (workflows.has(id as "pay") ? workflows(id as "pay") : id),
  };
}

/**
 * The right side of the inbox: what is being approved, why, and the decision buttons.
 * Action first (brief 5.3): title, state and the buttons, then the payload ("what will
 * happen"), the evidence, the chain and the history.
 */
export function ApprovalPanel({
  summary,
  state,
  meId,
  optimistic,
  stepUp,
  totpEnabled,
  returnPath,
  onDecide,
  onBack,
  headingId,
}: {
  summary: ApprovalSummary | null;
  state: DetailState;
  meId: string;
  optimistic: Optimistic | null;
  /** The API answered `second_factor_required` for this item. */
  stepUp: boolean;
  totpEnabled: boolean;
  returnPath: string;
  onDecide: (decision: DecisionChoice) => void;
  onBack: () => void;
  headingId: string;
}) {
  const t = useTranslations("Inbox");
  const format = useFormatter();
  const labels = useLabels();
  // "Show on the page" in the warning: the viewer scrolls to that finding (this item only).
  const [focus, setFocus] = useState<{ item: string; id: string; seq: number } | null>(null);
  const detail = state.status === "ready" ? state.detail : null;
  const base = detail ?? summary;

  if (!base) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground">
        {t("pickOne")}
      </div>
    );
  }

  const step = base.current_step;
  const stepIndex = detail && step ? detail.steps.findIndex((s) => s.id === step.id) + 1 : null;
  const status = optimistic?.state === "done" ? decidedStatus(optimistic.decision) : base.status;
  const decidable = !!detail && detail.can_decide && detail.status === "pending" && !optimistic;
  const needsFactor = !!detail && detail.status === "pending" && (stepUp || detail.needs_second_factor);
  const context = detail ? readContext(detail.context) : null;
  const findings = context ? guardrailHighlights(context.highlights) : [];
  const subject = subjectFor(base.workflow);

  return (
    <article aria-labelledby={headingId} className="flex min-h-full flex-col">
      <header className="space-y-3 border-b px-5 py-5 sm:px-8">
        <Button variant="ghost" size="sm" className="-ms-2 lg:hidden" onClick={onBack}>
          <ArrowLeft className="rtl:rotate-180" aria-hidden />
          {t("backToList")}
        </Button>
        <p className="eyebrow">
          {labels.workflow(base.workflow)} · <bdi>{base.approval_type}</bdi>
        </p>
        <h2 id={headingId} tabIndex={-1} className="font-display text-2xl leading-tight outline-none sm:text-3xl">
          <bdi>{base.title}</bdi>
        </h2>
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <ApprovalStatusBadge status={status} />
          {status === "pending" && <SlaBadge state={base.sla_state} />}
          {step && (
            <span>
              {stepIndex && detail
                ? t("stepOf", { step: stepIndex, count: detail.steps.length, name: step.name })
                : t("step", { name: step.name })}
            </span>
          )}
          {step?.due_at && status === "pending" && (
            <span>· {t("due", { when: format.relativeTime(new Date(step.due_at), new Date()) })}</span>
          )}
        </div>

        {/* What a reviewer must see before deciding: the guardrail findings. */}
        {findings.length > 0 && (
          <GuardrailNotice
            highlights={findings}
            subject={subject}
            className="mt-1"
            onShow={
              context?.documentId
                ? (h) => setFocus((previous) => ({ item: base.id, id: h.id, seq: (previous?.seq ?? 0) + 1 }))
                : undefined
            }
          />
        )}

        {/* Decision actions */}
        {optimistic && (
          <p role="status" className="flex items-center gap-2 text-sm">
            {optimistic.state === "sending" ? (
              <Loader2 className="size-4 animate-spin text-muted-foreground" aria-hidden />
            ) : (
              <Check className="size-4 text-success-ink" aria-hidden />
            )}
            {t(`optimistic.${optimistic.decision}.${optimistic.state}`)}
          </p>
        )}
        {needsFactor && !optimistic && (
          <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} className="mt-2" />
        )}
        {decidable && !needsFactor && (
          <div className="flex flex-wrap gap-2 pt-1">
            <Button onClick={() => onDecide("approve")} aria-keyshortcuts="A">
              <Check aria-hidden />
              {t("approve")}
              <Kbd className="ms-1 border-primary-foreground/30 bg-transparent text-primary-foreground/80">A</Kbd>
            </Button>
            <Button
              variant="outline"
              className="border-destructive/40 text-danger-ink hover:bg-destructive/10 hover:text-danger-ink"
              onClick={() => onDecide("reject")}
              aria-keyshortcuts="R"
            >
              <X aria-hidden />
              {t("reject")}
              <Kbd className="ms-1">R</Kbd>
            </Button>
            <Button variant="ghost" onClick={() => onDecide("request_changes")} aria-keyshortcuts="C">
              <RotateCcw aria-hidden />
              {t("requestChanges")}
              <Kbd className="ms-1">C</Kbd>
            </Button>
          </div>
        )}
        {detail && !detail.can_decide && detail.status === "pending" && !needsFactor && !optimistic && (
          <p className="text-sm text-muted-foreground">{t("cannotDecide")}</p>
        )}
      </header>

      <div className="flex-1 space-y-8 px-5 py-6 sm:px-8">
        {state.status === "loading" && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {t("loadingDetail")}
          </p>
        )}
        {state.status === "error" && (
          <p role="alert" className="text-sm text-danger-ink">
            {state.code === "not_found" ? t("notFound") : t("loadError")}
          </p>
        )}

        {detail && (
          <>
            <Section title={t("whatHappens")} id={`${headingId}-payload`}>
              {(detail.masked_fields ?? []).length > 0 && (
                <p
                  data-blind-masked
                  className="flex items-start gap-2 rounded-xl border border-dashed border-signal/50 bg-signal-soft/60 p-3 text-sm"
                >
                  <EyeOff className="mt-0.5 size-4 shrink-0" aria-hidden />
                  <span>{t("blindMasked")}</span>
                </p>
              )}
              <div className="rounded-xl border bg-surface p-4">
                <JsonView value={detail.payload} />
              </div>
            </Section>

            {isPayContext(detail.context) && (
              <Section title={t("payEvidence")} id={`${headingId}-pay`}>
                <div className="rounded-xl border bg-surface p-4">
                  <PayApprovalEvidence context={detail.context} />
                </div>
              </Section>
            )}

            {context?.documentId && (
              <Section title={t("evidence")} id={`${headingId}-evidence`}>
                <DocumentViewer
                  documentId={context.documentId}
                  highlights={context.highlights}
                  subject={subject}
                  focus={focus?.item === base.id ? focus : null}
                />
                <Link
                  href={`/documents/${context.documentId}?approval=${detail.id}`}
                  className="inline-flex items-center gap-1.5 text-sm font-medium text-primary underline-offset-4 hover:underline"
                >
                  <FileText className="size-4" aria-hidden />
                  {t("openDocument")}
                </Link>
              </Section>
            )}

            {Object.keys(detail.context).length > 0 && (
              <Section title={t("context")} id={`${headingId}-context`}>
                <div className="rounded-xl border bg-surface p-4">
                  <JsonView
                    value={detail.context}
                    renderValue={(key) => {
                      if (key === "evidence" || key === "guardrail_findings") {
                        return context?.documentId ? <span className="text-muted-foreground">{t("shownAbove")}</span> : undefined;
                      }
                      if ((key === "decision_id" || key === "decision_ids") && context?.decisionIds.length) {
                        return (
                          <span className="flex flex-wrap gap-2">
                            {context.decisionIds.map((id) => (
                              <Link
                                key={id}
                                href={`/decisions/${id}`}
                                className="inline-flex items-center gap-1 font-mono text-xs text-primary underline-offset-4 hover:underline"
                              >
                                {id}
                                <ExternalLink className="size-3" aria-hidden />
                              </Link>
                            ))}
                          </span>
                        );
                      }
                      return undefined;
                    }}
                  />
                </div>
              </Section>
            )}

            <Section title={t("chain")} id={`${headingId}-chain`}>
              <ol className="space-y-2">
                {detail.steps.map((s) => (
                  <li
                    key={s.id}
                    className={cn(
                      "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-surface px-4 py-2.5 text-sm",
                      s.id === step?.id && detail.status === "pending" && "border-signal/60",
                    )}
                  >
                    <span className="font-mono text-xs text-muted-foreground" dir="ltr">
                      {s.position + 1}
                    </span>
                    <span className="font-medium">
                      <bdi>{s.name}</bdi>
                    </span>
                    <Badge variant="outline">{t(`stepStatus.${knownStepStatus(s.status)}`)}</Badge>
                    <span className="text-muted-foreground">{t(`mode.${s.mode === "all_of" ? "all_of" : "any_of"}`)}</span>
                    {s.assignee_ids.includes(meId) && <Badge variant="signal">{t("assignedToYou")}</Badge>}
                  </li>
                ))}
              </ol>
            </Section>

            <Section title={t("history")} id={`${headingId}-history`}>
              {detail.history.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("noHistory")}</p>
              ) : (
                <ol className="space-y-3 border-s ps-4">
                  {detail.history.map((row) => (
                    <li key={row.id} className="space-y-1 text-sm">
                      <p>
                        <span className="font-medium">
                          {row.actor_user_id === meId ? t("you") : t("someone")}
                        </span>{" "}
                        {t(`decided.${knownDecision(row.decision)}`)}
                        {row.via_escalation && <span className="text-muted-foreground"> · {t("viaEscalation")}</span>}
                        {row.on_behalf_of && <span className="text-muted-foreground"> · {t("onBehalf")}</span>}
                      </p>
                      {row.reason && (
                        <p className="rounded-md bg-surface-sunken px-3 py-2 text-muted-foreground">
                          <bdi className="whitespace-pre-wrap">{row.reason}</bdi>
                        </p>
                      )}
                      <p className="text-xs text-muted-foreground">
                        {format.dateTime(new Date(row.created_at), { dateStyle: "medium", timeStyle: "short" })} ·{" "}
                        {t(`channel.${knownChannel(row.channel)}`)}
                      </p>
                    </li>
                  ))}
                </ol>
              )}
            </Section>

            <Section title={t("details")} id={`${headingId}-meta`}>
              <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[minmax(8rem,14rem)_1fr]">
                <dt className="text-muted-foreground">{t("created")}</dt>
                <dd>{format.dateTime(new Date(detail.created_at), { dateStyle: "medium", timeStyle: "short" })}</dd>
                <dt className="text-muted-foreground">{t("submittedBy")}</dt>
                <dd>
                  {detail.submitted_by_type === "user" && detail.submitted_by_id === meId
                    ? t("you")
                    : t(`actorType.${knownActorType(detail.submitted_by_type)}`)}
                </dd>
                <dt className="text-muted-foreground">{t("rules")}</dt>
                <dd>
                  <bdi>{detail.chain_name}</bdi>
                  {detail.policy_version !== null && <span className="text-muted-foreground"> · v{detail.policy_version}</span>}
                </dd>
                <dt className="text-muted-foreground">{t("payloadHash")}</dt>
                <dd className="truncate font-mono text-xs" dir="ltr" title={detail.payload_hash}>
                  {detail.payload_hash}
                </dd>
              </dl>
            </Section>
          </>
        )}
      </div>
    </article>
  );
}

export function decidedStatus(decision: DecisionChoice): string {
  return decision === "approve" ? "approved" : decision === "reject" ? "rejected" : "changes_requested";
}

const knownStepStatus = (s: string) =>
  (["waiting", "active", "approved", "rejected", "changes_requested", "cancelled"] as const).find((k) => k === s) ??
  "other";
const knownDecision = (s: string) => (["approve", "reject", "request_changes"] as const).find((k) => k === s) ?? "other";
const knownChannel = (s: string) => (["web", "email_link", "slack_link"] as const).find((k) => k === s) ?? "other";
const knownActorType = (s: string) => (["user", "agent", "system", "api_key"] as const).find((k) => k === s) ?? "system";
