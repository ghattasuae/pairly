// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlertTriangle, Check, Loader2, Pencil, ScanSearch, X } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type * as React from "react";

import { DocumentViewer } from "@/components/document/document-viewer";
import { GuardrailNotice } from "@/components/document/guardrail-notice";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ConfidenceMeter, type Tone } from "@/components/ui/status";
import { Section } from "@/components/ui/section";
import { Link } from "@/i18n/navigation";
import { correctInvoiceField, invoiceAction } from "@/lib/actions/pay";
import { highlightsFromFields, highlightsFromFindings } from "@/lib/evidence";
import {
  displayValue,
  fieldName,
  firstToReview,
  groupFields,
  highlightId,
  moveIndex,
  needsLook,
  percent,
  reviewOrder,
  textPieces,
  type ReviewField,
} from "@/lib/pay/invoices";
import { reasonParts } from "@/lib/pay/checks";
import type { PayCheck, PayInvoiceDetail } from "@/lib/pay/types";
import { cn } from "@/lib/utils";

import { ErrorText, VendorStatusBadge } from "./common";
import { InvoiceApprovalPanel } from "./invoice-approval";
import { ComparePanel, InvoiceChecks } from "./invoice-checks";

const KNOWN_REASONS = new Set([
  "extraction",
  "guardrails_flagged",
  "guardrails_error",
  "vendor_not_found",
  "vendor_weak_match",
  "vendor_not_active",
  "vendor_conflict",
  "vendor_linked_by_person",
  "document_not_parsed",
  "intake_unsure",
  "corrected_after_ready",
  "not_read",
  "run_ended",
  "check_failed",
  "check",
  "check_error",
  "risk",
  "coded_after_ready",
  "approval_blocked",
  "approval_refused",
  "approval_not_possible",
  "approval_not_started",
  "changes_requested_by_approver",
  "credit_note",
  "portal_vendor_mismatch",
]);

export function InvoiceStatusBadge({ status }: { status: string }) {
  const t = useTranslations("Pay.invoiceStatus");
  // Status reads in the semantic tones: work for a person (warning), waiting on
  // approvers (info), done (success), refused (danger). Saffron stays for actions.
  const variant =
    status === "needs_review"
      ? "warning"
      : status === "awaiting_approval" || status === "scheduled"
        ? "info"
        : status === "approved" || status === "paid"
          ? "success"
          : status === "rejected"
            ? "danger"
            : status === "ready"
              ? "default"
              : "outline";
  return (
    <Badge variant={variant} data-invoice-status={status}>
      {t(status as "ready")}
    </Badge>
  );
}

/**
 * Why a person must look, in plain words. A check reason reuses the explanation of the
 * finding behind it (or a plain sentence per code when the finding is not in the
 * detail); the code itself stays as secondary text with the raw reason as a tooltip.
 */
export function ReviewReasons({
  reasons,
  checks = [],
}: {
  reasons: readonly string[];
  checks?: readonly PayCheck[];
}) {
  const t = useTranslations("Pay.reasons");
  const codes = useTranslations("Pay.reasonCodes");
  const locale = useLocale();
  if (reasons.length === 0) return null;
  return (
    <ul className="space-y-1 text-sm" data-review-reasons>
      {reasons.map((reason) => {
        const { kind, code, explanation } = reasonParts(reason, checks, locale);
        const known = KNOWN_REASONS.has(kind);
        const text =
          explanation ??
          (kind === "check" && code && codes.has(code as "bank_mismatch")
            ? codes(code as "bank_mismatch")
            : known
              ? t(kind as "not_read")
              : reason);
        return (
          <li key={reason} className="flex items-start gap-2" data-reason={reason}>
            <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-warning-ink" />
            <span>
              {text}
              {(known || explanation) && code && (
                <span
                  className="ms-1 font-mono text-xs text-muted-foreground"
                  dir="ltr"
                  title={reason}
                  data-reason-code
                >
                  ({code})
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function FieldLabel({ path }: { path: string }) {
  const t = useTranslations("Pay.fields");
  const name = fieldName(path);
  return <>{t.has(name as "total") ? t(name as "total") : name}</>;
}

interface EditState {
  path: string;
  value: string;
  reason: string;
}

/**
 * The side-by-side review (brief 4.3.3): the invoice on the left with where each value
 * was read, the fields on the right. Doubtful fields are marked; selecting a field
 * shows its source. Keyboard: arrow keys or j and k move between fields, Enter shows
 * the source, e edits, Escape cancels. Corrections need a reason and are recorded.
 */
export function InvoiceReview({
  initial,
  vendors,
}: {
  initial: PayInvoiceDetail;
  vendors: { id: string; legal_name: string; reference: string }[];
}) {
  const t = useTranslations("Pay.invoice");
  const format = useFormatter();
  const ids = useId();
  const [invoice, setInvoice] = useState(initial);
  const fields = useMemo(() => (invoice.extraction?.fields ?? []) as ReviewField[], [invoice]);
  const groups = useMemo(() => groupFields(fields), [fields]);
  const order = useMemo(() => reviewOrder(groups), [groups]);
  const [active, setActive] = useState(() => firstToReview(order));
  const [focus, setFocus] = useState<{ id: string; seq: number } | null>(null);
  const [edit, setEdit] = useState<EditState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [why, setWhy] = useState("");
  const [vendorId, setVendorId] = useState("");
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const textRef = useRef<HTMLDivElement | null>(null);

  const document = invoice.document;
  const drawn = document.media_type === "application/pdf" || document.media_type.startsWith("image/");
  const highlights = useMemo(
    () => [...highlightsFromFields(fields), ...highlightsFromFindings(invoice.findings)],
    [fields, invoice.findings],
  );
  const activeField = order[active] ?? null;
  const canEdit = invoice.can_process && (invoice.status === "needs_review" || invoice.status === "ready");

  const show = useCallback((index: number, moveFocus = true) => {
    const target = order[index];
    if (!target) return;
    setActive(index);
    setFocus((f) => ({ id: highlightId(target.path), seq: (f?.seq ?? 0) + 1 }));
    if (moveFocus) buttons.current.get(target.path)?.focus();
  }, [order]);

  // The text view (no pages): scroll the marked source into view.
  useEffect(() => {
    if (drawn || !activeField) return;
    const mark = textRef.current?.querySelector<HTMLElement>(`[data-source~="${CSS.escape(activeField.path)}"]`);
    mark?.scrollIntoView?.({ block: "center" });
  }, [drawn, activeField, focus]);

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (edit || event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) {
      if (event.key === "Escape") setEdit(null);
      return;
    }
    const next = moveIndex(event.key, active, order.length);
    if (next !== null) {
      event.preventDefault();
      show(next);
      return;
    }
    if (event.key === "e" && canEdit && activeField) {
      event.preventDefault();
      startEdit(activeField);
    }
  }

  function startEdit(field: ReviewField) {
    setError(null);
    setEdit({ path: field.path, value: displayValue(field.current_value), reason: "" });
  }

  async function saveEdit(event: React.FormEvent) {
    event.preventDefault();
    if (!edit) return;
    setBusy("edit");
    setError(null);
    const result = await correctInvoiceField(invoice.id, {
      field_path: edit.path,
      new_value: edit.value.trim() === "" ? null : edit.value.trim(),
      reason: edit.reason,
    });
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setInvoice(result.invoice);
    const path = edit.path;
    setEdit(null);
    requestAnimationFrame(() => buttons.current.get(path)?.focus());
  }

  async function act(action: "review" | "reject" | "vendor", text: string) {
    setBusy(action);
    setError(null);
    const result = await invoiceAction(invoice.id, action, text);
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setInvoice(result.invoice);
    setRejecting(false);
    setWhy("");
  }

  const renderField = (field: ReviewField) => {
    const index = order.indexOf(field);
    const doubtful = needsLook(field);
    const isActive = index === active;
    const editing = edit?.path === field.path;
    const missing = field.flags.includes("missing");
    // A subtle semantic tint per field: doubtful (warning), missing (danger), corrected by
    // a person (info), read with high confidence (success), else plain paper.
    const tone: Tone = doubtful
      ? missing
        ? "danger"
        : "warning"
      : field.corrected
        ? "info"
        : field.confidence >= 0.9
          ? "success"
          : "neutral";
    return (
      <li key={field.path} data-field={field.path} data-needs-look={doubtful || undefined} data-tone={tone}>
        <div
          className={cn(
            "rounded-md border border-border border-s-[3px] bg-surface p-2.5 text-sm",
            tone === "danger" && "border-destructive/40 border-s-destructive bg-danger-soft/60",
            tone === "warning" && "border-warning-ink/30 border-s-warning-ink bg-warning-soft/60",
            tone === "info" && "border-s-info",
            tone === "success" && "border-s-success/70",
            tone === "neutral" && "border-s-rule",
            isActive && "ring-2 ring-ring/50",
          )}
        >
          <div className="flex items-start gap-2">
            <button
              type="button"
              ref={(el) => {
                if (el) buttons.current.set(field.path, el);
                else buttons.current.delete(field.path);
              }}
              aria-current={isActive ? "true" : undefined}
              tabIndex={isActive ? 0 : -1}
              onClick={() => show(index, false)}
              onFocus={() => setActive(index)}
              className="min-w-0 flex-1 text-start outline-none focus-visible:underline"
            >
              <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                <FieldLabel path={field.path} />
                {field.corrected && <Badge variant="info">{t("corrected")}</Badge>}
                {doubtful && (
                  <Badge variant={missing ? "danger" : "warning"} data-low-confidence>
                    {field.flags.includes("missing") ? t("missing") : t("check")}
                  </Badge>
                )}
              </span>
              <span className="mt-0.5 block break-words font-medium">
                {displayValue(field.current_value) ? (
                  <bdi>{displayValue(field.current_value)}</bdi>
                ) : (
                  <span className="text-muted-foreground">{t("empty")}</span>
                )}
              </span>
              <span className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                <ConfidenceMeter value={field.confidence} tone={tone === "info" ? "neutral" : tone}>
                  {t("confidence", { value: percent(field.confidence) })}
                </ConfidenceMeter>
                {field.spans.length === 0 && <span>{t("noSource")}</span>}
                {field.flags.length > 0 && !field.corrected && (
                  <span>{field.flags.map((f) => t.has(`flag.${f}` as "flag.missing") ? t(`flag.${f}` as "flag.missing") : f).join(", ")}</span>
                )}
              </span>
            </button>
            {canEdit && !editing && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-8 shrink-0"
                aria-label={t("edit")}
                onClick={() => startEdit(field)}
              >
                <Pencil aria-hidden />
              </Button>
            )}
          </div>
          {editing && edit && (
            <form onSubmit={saveEdit} className="mt-2 space-y-2" data-edit-form>
              <Label htmlFor={`${ids}-value`}>{t("newValue")}</Label>
              <Input
                id={`${ids}-value`}
                autoFocus
                dir="auto"
                value={edit.value}
                onChange={(e) => setEdit({ ...edit, value: e.target.value })}
              />
              <Label htmlFor={`${ids}-reason`}>{t("reason")}</Label>
              <Input
                id={`${ids}-reason`}
                required
                maxLength={1000}
                value={edit.reason}
                onChange={(e) => setEdit({ ...edit, reason: e.target.value })}
              />
              <div className="flex gap-2">
                <Button type="submit" size="sm" disabled={busy === "edit" || !edit.reason.trim()}>
                  {busy === "edit" ? <Loader2 aria-hidden className="animate-spin" /> : <Check aria-hidden />}
                  {t("save")}
                </Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setEdit(null)}>
                  <X aria-hidden />
                  {t("cancel")}
                </Button>
              </div>
            </form>
          )}
        </div>
      </li>
    );
  };

  const doubtfulCount = order.filter(needsLook).length;

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_27rem] lg:gap-8" data-invoice-review={invoice.status}>
      <div className="min-w-0 space-y-3 lg:sticky lg:top-20 lg:self-start">
        {drawn ? (
          <DocumentViewer documentId={document.id} highlights={highlights} focus={focus} subject="invoice" notice />
        ) : document.text ? (
          <>
          <GuardrailNotice highlights={highlightsFromFindings(invoice.findings)} subject="invoice" headingLevel={2} />
          <div
            ref={textRef}
            role="document"
            aria-label={t("text")}
            tabIndex={0}
            dir="auto"
            className="max-h-[75vh] overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface p-5 font-mono text-sm leading-relaxed"
            data-text-view
          >
            {textPieces(document.text, fields).map((piece, i) =>
              piece.paths.length ? (
                <mark
                  key={i}
                  data-source={piece.paths.join(" ")}
                  className={cn(
                    // The shared evidence mark (saffron-soft with a bottom rule); the field
                    // being looked at also gets a slate ring.
                    "evidence-mark",
                    activeField && piece.paths.includes(activeField.path) && "ring-1 ring-primary",
                  )}
                >
                  {piece.text}
                </mark>
              ) : (
                <span key={i}>{piece.text}</span>
              ),
            )}
          </div>
          </>
        ) : (
          <p className="card-hairline p-6 text-sm text-muted-foreground">{t("noText")}</p>
        )}
        <ComparePanel compare={invoice.compare ?? []} />
        <Link href={`/documents/${document.id}`} className="inline-flex items-center gap-1.5 text-sm text-primary underline-offset-4 hover:underline">
          <ScanSearch aria-hidden className="size-4" />
          {t("openDocument")}
        </Link>
      </div>

      <div className="min-w-0 space-y-5" onKeyDown={onKeyDown}>
        <section className="card-hairline space-y-3 p-5" aria-labelledby={`${ids}-summary`}>
          <h2 id={`${ids}-summary`} className="sr-only">
            {t("summary")}
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <InvoiceStatusBadge status={invoice.status} />
            {invoice.extraction?.source === "einvoice" && <Badge variant="outline">{t("einvoice")}</Badge>}
            {invoice.extraction?.from_recording && <Badge variant="outline">{t("recorded")}</Badge>}
          </div>
          <p className="text-sm">
            {invoice.vendor ? (
              <>
                <Link href={`/pay/vendors/${invoice.vendor.id}`} className="font-medium underline-offset-4 hover:underline">
                  <bdi>{invoice.vendor.legal_name}</bdi>
                </Link>{" "}
                <VendorStatusBadge status={invoice.vendor.status} />
                {typeof invoice.vendor_match.method === "string" && (
                  <span className="ms-2 text-xs text-muted-foreground">
                    {t(`match.${invoice.vendor_match.method}` as "match.trn")}
                  </span>
                )}
              </>
            ) : (
              <span className="text-muted-foreground">{t("noVendor")}</span>
            )}
          </p>
          {invoice.total && invoice.currency && (
            <p className="figure text-4xl leading-none" dir="ltr" data-amount>
              {invoice.currency} {invoice.total}
            </p>
          )}
          {invoice.email && (
            <p className="text-xs text-muted-foreground">
              {t("receivedByEmail", { reference: invoice.email.reference })}
            </p>
          )}
          <p className="font-mono text-xs text-muted-foreground">
            {format.dateTime(new Date(invoice.created_at), { dateStyle: "medium", timeStyle: "short" })}
          </p>
          <ReviewReasons reasons={invoice.review_reasons} checks={invoice.checks ?? []} />
        </section>

        <InvoiceChecks checks={invoice.checks ?? []} risk={invoice.risk} />

        <InvoiceApprovalPanel invoice={invoice} onChange={setInvoice} />

        {invoice.extraction ? (
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground" aria-live="polite">
              {doubtfulCount > 0 ? t("toCheck", { count: doubtfulCount }) : t("allChecked")}
            </p>
            <p className="text-xs text-muted-foreground">{t("keys")}</p>
            <section aria-labelledby={`${ids}-header`} className="space-y-2">
              <h2 id={`${ids}-header`} className="eyebrow">
                {t("header")}
              </h2>
              <ul className="space-y-2">{groups.header.map(renderField)}</ul>
            </section>
            {groups.bank.length > 0 && (
              <section aria-labelledby={`${ids}-bank`} className="space-y-2">
                <h2 id={`${ids}-bank`} className="eyebrow">
                  {t("bank")}
                </h2>
                <p className="text-xs text-muted-foreground">{t("bankNote")}</p>
                <ul className="space-y-2">{groups.bank.map(renderField)}</ul>
              </section>
            )}
            <section aria-labelledby={`${ids}-lines`} className="space-y-3">
              <h2 id={`${ids}-lines`} className="eyebrow">
                {t("lines", { count: groups.lines.length })}
              </h2>
              {groups.lines.map((line) => (
                <div key={line.index} className="space-y-2" data-line={line.index}>
                  <h3 className="text-sm font-medium">{t("line", { number: line.index + 1 })}</h3>
                  <ul className="space-y-2">{line.fields.map(renderField)}</ul>
                </div>
              ))}
            </section>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{t("notRead")}</p>
        )}

        <ErrorText code={error} />

        {invoice.can_process && (invoice.status === "needs_review" || invoice.status === "ready") && (
          <Section aria-labelledby={`${ids}-actions`} title={<span id={`${ids}-actions`}>{t("actions")}</span>}>
            {(invoice.vendor === null || invoice.vendor_match.conflict === true) && vendors.length > 0 && (
              <form
                className="flex flex-wrap items-end gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (vendorId) void act("vendor", vendorId);
                }}
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <Label htmlFor={`${ids}-vendor`}>{t("linkVendor")}</Label>
                  <Select id={`${ids}-vendor`} value={vendorId} onChange={(e) => setVendorId(e.target.value)}>
                    <option value="">{t("chooseVendor")}</option>
                    {vendors.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.legal_name} ({v.reference})
                      </option>
                    ))}
                  </Select>
                </div>
                <Button type="submit" size="sm" variant="outline" disabled={!vendorId || busy !== null}>
                  {t("link")}
                </Button>
              </form>
            )}
            <div className="flex flex-wrap gap-2">
              {invoice.status === "needs_review" && (
                <Button type="button" size="sm" disabled={busy !== null} onClick={() => void act("review", "")}>
                  {busy === "review" ? <Loader2 aria-hidden className="animate-spin" /> : <Check aria-hidden />}
                  {t("markReviewed")}
                </Button>
              )}
              <Button type="button" size="sm" variant="outline" onClick={() => setRejecting((r) => !r)}>
                {t("reject")}
              </Button>
            </div>
            {rejecting && (
              <form
                className="space-y-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  void act("reject", why);
                }}
              >
                <Label htmlFor={`${ids}-why`}>{t("rejectReason")}</Label>
                <Textarea id={`${ids}-why`} required maxLength={1000} value={why} onChange={(e) => setWhy(e.target.value)} />
                <Button type="submit" size="sm" variant="destructive" disabled={!why.trim() || busy !== null}>
                  {t("confirmReject")}
                </Button>
              </form>
            )}
          </Section>
        )}
      </div>
    </div>
  );
}
