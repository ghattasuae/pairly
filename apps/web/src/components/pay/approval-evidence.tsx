// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlertTriangle } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Link } from "@/i18n/navigation";

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : typeof value === "number" ? String(value) : null;
}

const FIELDS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "subtotal", "vat_amount", "total", "department", "category"] as const;
const SAFE_PATH = /^\/pay\/(invoices|payment-runs)\/[0-9a-f-]{36}$/;

/** Whether the inbox has a Pay view for this approval context. */
export function isPayContext(context: unknown): boolean {
  return isObject(context) && (context.kind === "pay.invoice" || context.kind === "pay.payment_run");
}

/**
 * What an approver must see before deciding a Pay approval (brief 4.6.2): for an
 * invoice, the values, the findings that need a person, the risk with its explanation,
 * the vendor's risk and any suspected originals (the invoice page shows them side by
 * side); for a payment run, every invoice in it and what was left out.
 */
export function PayApprovalEvidence({ context }: { context: unknown }) {
  const t = useTranslations("Pay.evidence");
  const tFields = useTranslations("Pay.fields");
  const locale = useLocale();
  if (!isObject(context)) return null;

  if (context.kind === "pay.payment_run") {
    const items = Array.isArray(context.items) ? context.items.filter(isObject) : [];
    const excluded = Array.isArray(context.excluded) ? context.excluded.filter(isObject) : [];
    const path = text(context.payment_run_path);
    return (
      <div className="space-y-3 text-sm" data-pay-evidence="payment_run">
        <ul className="divide-y rounded-lg border">
          {items.map((item, i) => (
            <li key={i} className="flex flex-wrap justify-between gap-2 px-3 py-2">
              <span>
                {text(item.invoice_reference)} <bdi className="text-muted-foreground">{text(item.vendor)}</bdi>
              </span>
              <span className="font-mono tabular-nums" dir="ltr">
                {text(item.amount)}
              </span>
            </li>
          ))}
        </ul>
        {excluded.length > 0 && <p className="text-muted-foreground">{t("leftOut", { count: excluded.length })}</p>}
        {path && SAFE_PATH.test(path) && (
          <Link href={path} className="text-primary underline-offset-4 hover:underline">
            {t("openRun")}
          </Link>
        )}
      </div>
    );
  }

  if (context.kind !== "pay.invoice") return null;
  const fields = isObject(context.fields) ? context.fields : {};
  const risk = isObject(context.risk) ? context.risk : {};
  const vendor = isObject(context.vendor) ? context.vendor : {};
  const findings = (Array.isArray(context.findings) ? context.findings.filter(isObject) : []).filter(
    (f) => f.needs_review === true,
  );
  const compare = Array.isArray(context.compare) ? context.compare.filter(isObject) : [];
  const explanation = isObject(risk.explanation) ? risk.explanation : {};
  const level = text(risk.level) ?? "critical";
  const path = text(context.invoice_path);
  return (
    <div className="space-y-4 text-sm" data-pay-evidence="invoice">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        {FIELDS.filter((f) => text(fields[f])).map((f) => (
          <div key={f} className="contents">
            <dt className="text-muted-foreground">{tFields.has(f) ? tFields(f) : f}</dt>
            <dd>
              <bdi>{text(fields[f])}</bdi>
            </dd>
          </div>
        ))}
      </dl>
      <div className="space-y-1" data-risk={level}>
        <p className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{t("risk")}</span>
          <Badge variant={level === "low" ? "default" : "signal"}>{t(`level.${level}` as "level.low")}</Badge>
          {text(vendor.risk) && (
            <span className="text-muted-foreground">{t("vendorRisk", { level: t(`level.${text(vendor.risk)}` as "level.low") })}</span>
          )}
        </p>
        {text(locale === "ar" ? explanation.ar : explanation.en) && (
          <p className="text-muted-foreground">{text(locale === "ar" ? explanation.ar : explanation.en)}</p>
        )}
      </div>
      {findings.length > 0 && (
        <ul className="space-y-1" data-evidence-findings>
          {findings.slice(0, 20).map((f, i) => {
            const message = isObject(f.message) ? f.message : {};
            return (
              <li key={i} className="flex items-start gap-2">
                <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-signal" />
                <span>{text(locale === "ar" ? message.ar : message.en) ?? text(f.code)}</span>
              </li>
            );
          })}
        </ul>
      )}
      {compare.length > 0 && <p data-evidence-compare>{t("suspectedOriginals", { count: compare.length })}</p>}
      {path && SAFE_PATH.test(path) && (
        <Link href={path} className="text-primary underline-offset-4 hover:underline">
          {t("openInvoice")}
        </Link>
      )}
    </div>
  );
}
