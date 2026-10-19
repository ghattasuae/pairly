// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Download, ShieldAlert, ShieldCheck } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type * as React from "react";

import { useExplanation } from "@/components/data/explain";
import { humanizeKey, isRedacted, JsonView, Redacted } from "@/components/data/json-view";
import { PageHeader } from "@/components/states/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import type { Appeal, Replay } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { AppealsPanel, ReplayPanel } from "./decision-actions";

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const asString = (value: unknown): string => (typeof value === "string" ? value : "");
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DECISION = /^dec_[0-9A-HJKMNP-TV-Z]{26}$/;

export interface DecisionViewProps {
  decisionId: string;
  /** The receipt JSON, already redacted by the API for this person's role. */
  receipt: Json;
  replays: Replay[];
  /** This decision's appeals, or null when the person cannot see appeals. */
  appeals: Appeal[] | null;
  can: { replay: boolean; fileAppeal: boolean; resolveAppeal: boolean };
  totpEnabled: boolean;
}

function Section({ id, title, children, className }: { id: string; title: string; children: React.ReactNode; className?: string }) {
  return (
    <section aria-labelledby={id} className={cn("space-y-3", className)}>
      <h2 id={id} className="eyebrow">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("card-hairline p-4", className)}>{children}</div>;
}

/** Confidence as a percentage and a quiet meter. */
function Confidence({ value }: { value: unknown }) {
  const t = useTranslations("Decision");
  if (typeof value !== "number") return <span className="text-muted-foreground">{t("noConfidence")}</span>;
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <span className="flex items-center gap-3">
      <span className="font-mono tabular-nums" dir="ltr">
        {pct}%
      </span>
      <span
        role="meter"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={t("confidence")}
        className="h-1.5 w-32 overflow-hidden rounded-full bg-muted"
      >
        <span className="block h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}

function Outcome({ outcome }: { outcome: string }) {
  const t = useTranslations("Decision.ruleOutcome");
  const known = ["pass", "fail", "error", "not_applicable", "skipped", "warn"].includes(outcome);
  return (
    <Badge
      variant="outline"
      className={cn(
        outcome === "pass" && "border-success/40 text-success-ink",
        outcome === "fail" && "border-destructive/40 text-danger-ink",
        outcome === "error" && "border-signal/60",
      )}
    >
      {known ? t(outcome as "pass") : outcome}
    </Badge>
  );
}

function Evidence({ evidence, workflow }: { evidence: unknown; workflow: string }) {
  const t = useTranslations("Decision");
  const explain = useExplanation();
  const severity = useTranslations("Severity");
  if (isRedacted(evidence)) return <Redacted />;
  if (!isObject(evidence) || Object.keys(evidence).length === 0) {
    return <p className="text-sm text-muted-foreground">{t("noEvidence")}</p>;
  }
  const { rule_results, risk_signals, guardrail_findings, quote_locations, ...rest } = evidence;
  const sev = (value: unknown) => {
    const s = asString(value);
    return s ? (severity.has(s as "high") ? severity(s as "high") : s) : null;
  };
  const subject = workflow === "pay" ? "invoice" : workflow === "hire" ? "cv" : "document";
  return (
    <div className="space-y-6">
      {asArray(rule_results).length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">{t("ruleResults")}</h3>
          <div className="relative overflow-x-auto card-hairline">
            <table className="w-full text-sm">
              <thead className="text-start text-xs text-muted-foreground">
                <tr className="border-b">
                  <th scope="col" className="px-4 py-2 text-start font-medium">{t("rule")}</th>
                  <th scope="col" className="px-4 py-2 text-start font-medium">{t("result")}</th>
                  <th scope="col" className="px-4 py-2 text-start font-medium">{t("severity")}</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {asArray(rule_results)
                  .filter(isObject)
                  .map((r, i) => (
                    <tr key={`${asString(r.rule_id)}:${i}`}>
                      <td className="px-4 py-2 font-mono text-xs" dir="ltr">
                        {asString(r.rule_id)}
                      </td>
                      <td className="px-4 py-2">
                        <Outcome outcome={asString(r.outcome)} />
                        {asString(r.error_code) && (
                          <span className="ms-2 font-mono text-xs text-muted-foreground">{asString(r.error_code)}</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-muted-foreground">{sev(r.severity) ?? "-"}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {asArray(risk_signals).length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">{t("riskSignals")}</h3>
          <ul className="space-y-2">
            {asArray(risk_signals)
              .filter(isObject)
              .map((s, i) => (
                <li key={`${asString(s.detector)}:${i}`} className="card-hairline p-3 text-sm">
                  <p className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{explain(asString(s.explanation_key))}</span>
                  </p>
                  <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                    <span className="font-mono" dir="ltr">
                      {asString(s.detector)}
                    </span>
                    {sev(s.severity) && <span>{t("severityIs", { severity: sev(s.severity) ?? "" })}</span>}
                    {typeof s.score === "number" && <span dir="ltr">{t("score", { score: s.score.toFixed(2) })}</span>}
                    {asString(s.status) && asString(s.status) !== "ok" && <span>{asString(s.status)}</span>}
                  </p>
                </li>
              ))}
          </ul>
        </div>
      )}
      {asArray(guardrail_findings).length > 0 && (
        <div className="space-y-2">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <ShieldAlert className="size-4 text-danger-ink" aria-hidden />
            {t("guardrailFindings")}
          </h3>
          <ul className="space-y-2">
            {asArray(guardrail_findings)
              .filter(isObject)
              .map((f, i) => (
                <li key={`${asString(f.id)}:${i}`} className="rounded-lg border border-dashed border-destructive/50 bg-surface p-3 text-sm">
                  <p>
                    {explain(`guardrails.${asString(f.type)}.${asString(f.code)}`, {
                      evidence: asString(f.evidence),
                      subject,
                    })}
                  </p>
                  <p className="mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                    {sev(f.severity) && <span>{t("severityIs", { severity: sev(f.severity) ?? "" })}</span>}
                    {typeof f.page === "number" && <span>{t("onPage", { page: f.page })}</span>}
                  </p>
                </li>
              ))}
          </ul>
        </div>
      )}
      {asArray(quote_locations).length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">{t("quoteLocations")}</h3>
          <ul className="flex flex-wrap gap-2">
            {asArray(quote_locations)
              .filter(isObject)
              .map((q, i) => (
                <li key={i} className="rounded-md border bg-surface px-2.5 py-1 text-xs">
                  {humanizeKey(asString(q.path))} · {t("onPage", { page: typeof q.page === "number" ? q.page : 1 })}
                </li>
              ))}
          </ul>
        </div>
      )}
      {Object.keys(rest).length > 0 && (
        <Card>
          <JsonView value={rest} />
        </Card>
      )}
    </div>
  );
}

/** Ids in a receipt become links: documents to the viewer, decisions to their page, approvals to the inbox. */
function linkedValue(key: string, value: unknown): React.ReactNode | undefined {
  const ids = Array.isArray(value) ? value : [value];
  if (!ids.every((v) => typeof v === "string") || ids.length === 0) return undefined;
  const strings = ids as string[];
  let href: ((id: string) => string) | null = null;
  if (/^document_ids?$/.test(key) && strings.every((s) => UUID.test(s))) href = (id) => `/documents/${id}`;
  else if (/^(supersedes|superseded_by|history)$/.test(key) && strings.every((s) => DECISION.test(s))) href = (id) => `/decisions/${id}`;
  else if (key === "approval_ids" && strings.every((s) => UUID.test(s))) href = (id) => `/inbox/${id}`;
  if (!href) return undefined;
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-1">
      {strings.map((id) => (
        <Link key={id} href={href(id)} className="font-mono text-xs text-primary underline-offset-4 hover:underline">
          {id}
        </Link>
      ))}
    </span>
  );
}

/** A decision receipt (brief 2.3.1 and 2.3.5) as a readable page, plus replay and appeals. */
export function DecisionView({ decisionId, receipt, replays, appeals, can, totpEnabled }: DecisionViewProps) {
  const t = useTranslations("Decision");
  const kinds = useTranslations("DecisionKinds");
  const workflows = useTranslations("Workflows");
  const format = useFormatter();

  const kind = asString(receipt.kind);
  const workflow = asString(receipt.workflow);
  const subject = isObject(receipt.subject) ? receipt.subject : {};
  const integrity = isObject(receipt.integrity) ? receipt.integrity : {};
  const model = isObject(receipt.model) ? receipt.model : null;
  const human = isObject(receipt.human_action) ? receipt.human_action : null;
  const actor = isObject(receipt.actor) ? receipt.actor : null;
  const links = isObject(receipt.links) ? receipt.links : null;
  const date = (value: unknown) =>
    typeof value === "string" && !Number.isNaN(Date.parse(value))
      ? format.dateTime(new Date(value), { dateStyle: "medium", timeStyle: "short" })
      : "-";
  const kindLabel = kinds.has(kind as "risk_score") ? kinds(kind as "risk_score") : humanizeKey(kind);
  const workflowLabel = workflows.has(workflow as "pay") ? workflows(workflow as "pay") : workflow;
  const policies = asArray(receipt.policy_versions).filter(isObject);
  const prompts = asArray(receipt.prompt_versions).filter(isObject);
  const intact = integrity.intact === true;

  return (
    <div className="page space-y-10">
      <PageHeader eyebrow={`${t("eyebrow")} · ${workflowLabel}`} title={kindLabel}>
        <div className="flex flex-wrap items-center gap-2 pt-1 text-sm">
          <code className="rounded bg-surface-sunken px-1.5 py-0.5 font-mono text-xs text-foreground" dir="ltr">
            {decisionId}
          </code>
          {intact ? (
            <Badge variant="outline" className="border-success/40 text-success-ink [&_svg]:size-3">
              <ShieldCheck aria-hidden />
              {t("intact")}
            </Badge>
          ) : (
            <Badge variant="outline" className="border-destructive/40 text-danger-ink [&_svg]:size-3">
              <ShieldAlert aria-hidden />
              {t("notIntact")}
            </Badge>
          )}
          {asString(receipt.audience) === "internal" && <Badge variant="outline">{t("personalHidden")}</Badge>}
        </div>
      </PageHeader>

      <div className="flex flex-wrap gap-2">
        <Button asChild variant="outline" size="sm">
          <a href={`/api/decisions/${decisionId}/receipt?lang=en`} download>
            <Download aria-hidden />
            {t("downloadEn")}
          </a>
        </Button>
        <Button asChild variant="outline" size="sm">
          <a href={`/api/decisions/${decisionId}/receipt?lang=ar`} download>
            <Download aria-hidden />
            {t("downloadAr")}
          </a>
        </Button>
      </div>

      <Section id="summary" title={t("summary")}>
        <Card>
          <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-[minmax(8rem,14rem)_1fr]">
            <dt className="text-muted-foreground">{t("about")}</dt>
            <dd>
              {asString(subject.type) === "document" && UUID.test(asString(subject.id)) ? (
                <Link href={`/documents/${asString(subject.id)}`} className="text-primary underline-offset-4 hover:underline">
                  {t("document")} <bdi className="font-mono text-xs">{asString(subject.id)}</bdi>
                </Link>
              ) : (
                <span>
                  {humanizeKey(asString(subject.type))} <bdi className="font-mono text-xs">{asString(subject.id)}</bdi>
                </span>
              )}
            </dd>
            <dt className="text-muted-foreground">{t("decidedAt")}</dt>
            <dd>{date(receipt.decided_at)}</dd>
            <dt className="text-muted-foreground">{t("recordedAt")}</dt>
            <dd>{date(receipt.recorded_at)}</dd>
            <dt className="text-muted-foreground">{t("confidence")}</dt>
            <dd>
              <Confidence value={receipt.confidence} />
            </dd>
            <dt className="text-muted-foreground">{t("humanAction")}</dt>
            <dd>
              {human ? (
                <span>
                  {humanizeKey(asString(human.action))}
                  {actor && (
                    <span className="text-muted-foreground">
                      {" "}
                      · {t(`actorType.${["user", "agent", "system", "api_key"].includes(asString(actor.type)) ? asString(actor.type) : "system"}` as "actorType.user")}
                    </span>
                  )}
                </span>
              ) : (
                <span className="text-muted-foreground">{t("noHumanAction")}</span>
              )}
            </dd>
            {model && (
              <>
                <dt className="text-muted-foreground">{t("model")}</dt>
                <dd>
                  <bdi className="font-mono text-xs">
                    {[asString(model.provider), asString(model.model)].filter(Boolean).join(" / ") || "-"}
                  </bdi>
                  {model.from_recording === true && <Badge variant="outline" className="ms-2">{t("fromRecording")}</Badge>}
                </dd>
              </>
            )}
            {asString(receipt.internal_note) && (
              <>
                <dt className="text-muted-foreground">{t("internalNote")}</dt>
                <dd className="whitespace-pre-wrap">
                  <bdi>{asString(receipt.internal_note)}</bdi>
                </dd>
              </>
            )}
            {isRedacted(receipt.internal_note) && (
              <>
                <dt className="text-muted-foreground">{t("internalNote")}</dt>
                <dd>
                  <Redacted />
                </dd>
              </>
            )}
          </dl>
        </Card>
      </Section>

      <Section id="outcome" title={t("outcome")}>
        <Card>
          <JsonView value={receipt.outcome} />
        </Card>
      </Section>

      <Section id="versions" title={t("versions")}>
        <div className="grid gap-4 md:grid-cols-2">
          <Card>
            <h3 className="mb-2 text-sm font-medium">{t("policyVersions")}</h3>
            {policies.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("none")}</p>
            ) : (
              <ul className="space-y-1.5 text-sm">
                {policies.map((p, i) => (
                  <li key={i} className="flex flex-wrap items-baseline gap-2">
                    <bdi className="font-medium">{asString(p.name)}</bdi>
                    <span className="text-muted-foreground">v{String(p.version ?? "")}</span>
                    <span className="font-mono text-xs text-muted-foreground" dir="ltr" title={asString(p.hash)}>
                      {asString(p.hash).slice(0, 12)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card>
            <h3 className="mb-2 text-sm font-medium">{t("promptVersions")}</h3>
            {prompts.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("none")}</p>
            ) : (
              <ul className="space-y-1.5 text-sm">
                {prompts.map((p, i) => (
                  <li key={i} className="flex flex-wrap items-baseline gap-2">
                    <bdi className="font-medium">{asString(p.task)}</bdi>
                    <span className="text-muted-foreground">{asString(p.version)}</span>
                    <span className="font-mono text-xs text-muted-foreground" dir="ltr" title={asString(p.hash)}>
                      {asString(p.hash).slice(0, 12)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </Section>

      <Section id="evidence" title={t("evidence")}>
        <Evidence evidence={receipt.evidence} workflow={workflow} />
      </Section>

      {"inputs" in receipt && (
        <Section id="inputs" title={t("inputs")}>
          <Card>
            <JsonView value={receipt.inputs} renderValue={linkedValue} />
          </Card>
        </Section>
      )}

      {"personal_data" in receipt && isObject(receipt.personal_data) && Object.keys(receipt.personal_data).length > 0 && (
        <Section id="personal" title={t("personalData")}>
          <Card>
            <JsonView value={receipt.personal_data} />
          </Card>
        </Section>
      )}

      {links && (
        <Section id="links" title={t("links")}>
          <Card>
            <JsonView
              value={Object.fromEntries(Object.entries(links).filter(([key]) => key !== "audit_event_ids"))}
              renderValue={linkedValue}
            />
            {asArray(links.audit_event_ids).length > 0 && (
              <p className="mt-3 text-sm">
                <Link
                  href={`/audit?entity_type=decision_record&entity_id=${decisionId}`}
                  className="text-primary underline-offset-4 hover:underline"
                >
                  {t("auditEvents", { count: asArray(links.audit_event_ids).length })}
                </Link>
              </p>
            )}
          </Card>
        </Section>
      )}

      <ReplayPanel decisionId={decisionId} initial={replays} canReplay={can.replay} totpEnabled={totpEnabled} />

      {appeals !== null && (
        <AppealsPanel
          decisionId={decisionId}
          initial={appeals}
          canFile={can.fileAppeal}
          canResolve={can.resolveAppeal}
          totpEnabled={totpEnabled}
        />
      )}
    </div>
  );
}
