// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { ArrowUpRight, CheckCircle2, CircleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { PayMetricRecords, PayMetricValue } from "@/components/pay/pay-analytics";
import { Link } from "@/i18n/navigation";
import {
  PAY_PAGE_LINKS,
  safeHref,
  type PayDashAmount,
  type PayDashboard,
  type PayDashboardKey,
  type PayDashItem,
  type PayDashSection,
} from "@/lib/pay/dashboards";
import type { PayMetric } from "@/lib/pay/types";

const SECTIONS = new Set([
  "to_review",
  "ready",
  "low_confidence",
  "duplicates",
  "waiting_on_vendor",
  "intake",
  "exceptions_by_category",
  "awaiting_me",
  "delegated",
  "history",
  "upcoming_runs",
  "discounts",
  "exceptions_trend",
  "month_end",
]);
const CATEGORIES = new Set([
  "no_po",
  "price_variance",
  "quantity_variance",
  "missing_receipt",
  "duplicate_suspect",
  "bank_mismatch",
  "policy_breach",
  "low_confidence",
  "other",
]);
const METRICS = new Set([
  "reviewed_7d",
  "reviewed_30d",
  "submitted_7d",
  "submitted_30d",
  "approved_this_month",
  "touchless_rate",
  "cycle.receipt_to_payment",
  "cost_per_invoice",
  "days_payable_outstanding",
  "bank_changes_blocked",
  "fraud_flags",
]);
const AMOUNTS = new Set([
  "payables",
  "forecast_4_weeks",
  "approved_this_month",
  "aging.current",
  "aging.1_30",
  "aging.31_60",
  "aging.61_90",
  "aging.90_plus",
  "aging.undated",
]);
const RISKS = new Set(["low", "medium", "high", "critical"]);
const SLAS = new Set(["on_track", "due_soon", "overdue"]);
const CHECKS = new Set(["journal_exported", "statements_matched", "exceptions_cleared"]);

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function To({ href, className, children, ...rest }: { href: string | null; className?: string; children: React.ReactNode } & Record<string, unknown>) {
  const safe = safeHref(href);
  if (!safe) return <span className={className}>{children}</span>;
  return (
    <Link href={safe} className={className} {...rest}>
      {children}
    </Link>
  );
}

function ItemLine({ item }: { item: PayDashItem }) {
  const t = useTranslations("PayDashboards");
  const format = useFormatter();
  const d = item.detail;
  const bits: string[] = [];
  let label = item.label;
  if (item.kind === "category" && CATEGORIES.has(item.id)) label = t(`category.${item.id as "other"}`);
  if (item.kind === "check" && CHECKS.has(item.id)) label = t(`check.${item.id as "journal_exported"}`);
  if (item.kind === "week") label = t("week", { start: format.dateTime(new Date(`${item.id}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" }) });
  const vendor = text(d.vendor);
  if (vendor) bits.push(vendor);
  if (text(d.total) && text(d.currency)) bits.push(`${d.currency} ${d.total}`);
  if (typeof d.count === "number" && item.kind !== "check") bits.push(t("count", { count: d.count }));
  if (text(d.saving) && text(d.pay_by)) bits.push(t("saving", { saving: `${d.currency ?? ""} ${d.saving}`, date: String(d.pay_by) }));
  if (text(d.execution_date)) bits.push(t("runsOn", { date: String(d.execution_date) }));
  if (typeof d.received === "number" && typeof d.with_exceptions === "number") {
    bits.push(t("trend", { received: d.received, flagged: d.with_exceptions }));
  }
  if (text(d.decision)) bits.push(t(`decision.${d.decision === "approve" ? "approve" : d.decision === "reject" ? "reject" : "other"}`));
  const risk = text(d.risk);
  const sla = text(d.sla);
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-0.5 py-1.5 text-sm" data-dash-item={item.kind}>
      {item.kind === "check" &&
        (d.done ? <CheckCircle2 className="size-4 text-primary" aria-hidden /> : <CircleAlert className="size-4 text-destructive" aria-hidden />)}
      <To href={item.href} className="font-medium hover:text-primary hover:underline" dir="auto">
        {label}
      </To>
      {item.kind === "check" && (
        <span className="text-xs text-muted-foreground" data-check-done={d.done ? "yes" : "no"}>
          {d.done ? t("done") : t("open", { count: typeof d.count === "number" ? d.count : 0 })}
        </span>
      )}
      {risk && RISKS.has(risk) && (
        <span className="rounded border px-1.5 text-xs" data-risk={risk}>
          {t(`risk.${risk as "low"}`)}
        </span>
      )}
      {sla && SLAS.has(sla) && (
        <span className={`text-xs ${sla === "overdue" ? "text-destructive" : "text-muted-foreground"}`} data-sla={sla}>
          {t(`sla.${sla as "on_track"}`)}
        </span>
      )}
      {bits.length > 0 && <span className="text-xs text-muted-foreground">{bits.join(" · ")}</span>}
    </li>
  );
}

function SectionCard({ board, section, prominent }: { board: PayDashboardKey; section: PayDashSection; prominent?: boolean }) {
  const t = useTranslations("PayDashboards");
  if (!SECTIONS.has(section.key)) return null;
  const key = section.key as "to_review";
  const id = `pay-dash-${board}-${section.key}`.replace(/_/g, "-");
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-24 space-y-2 rounded-lg border bg-surface p-5" data-pay-section={section.key}>
      <header className="flex items-baseline justify-between gap-2">
        <h3 id={`${id}-title`} className="text-sm font-medium">
          {t(`section.${key}`)}
        </h3>
        <To
          href={section.href}
          className={`figure leading-none ${prominent ? `text-3xl ${section.count > 0 ? "text-signal-ink" : ""}` : "text-2xl"} hover:text-primary hover:underline`}
          aria-label={t("countLink", { count: section.count, what: t(`section.${key}`) })}
          data-pay-count={section.count}
        >
          {section.count}
        </To>
      </header>
      {section.items.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t(prominent ? "emptyAction" : "emptySection")}</p>
      ) : (
        <ul className="divide-y">
          {section.items.map((item) => (
            <ItemLine key={`${item.kind}:${item.id}`} item={item} />
          ))}
        </ul>
      )}
      {section.count > section.items.length && section.href && (
        <p className="text-xs">
          <To href={section.href} className="inline-flex items-center gap-1 text-primary hover:underline">
            {t("seeAll", { count: section.count })}
            <ArrowUpRight className="size-3 rtl:-scale-x-100" aria-hidden />
          </To>
        </p>
      )}
    </section>
  );
}

function Amounts({ amounts }: { amounts: PayDashAmount[] }) {
  const t = useTranslations("PayDashboards");
  const shown = amounts.filter((a) => AMOUNTS.has(a.key));
  if (shown.length === 0) return null;
  return (
    <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {shown.map((a) => (
        <li key={`${a.key}:${a.currency}`} className="space-y-1.5 rounded-lg border bg-surface p-4" data-pay-amount={a.key}>
          <p className="text-xs text-muted-foreground">{t(`amount.${a.key as "payables"}`)}</p>
          <To href={a.href} className="text-xl font-medium hover:text-primary hover:underline">
            <span className="font-mono tabular-nums" dir="ltr">
              {a.currency} {a.amount}
            </span>
          </To>
          <p className="text-xs text-muted-foreground">{t("invoices", { count: a.count })}</p>
        </li>
      ))}
    </ul>
  );
}

function Metrics({ metrics }: { metrics: PayMetric[] }) {
  const t = useTranslations("PayDashboards");
  const shown = metrics.filter((m) => METRICS.has(m.key));
  if (shown.length === 0) return null;
  return (
    <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {shown.map((m) => (
        <li key={m.key} className="space-y-1.5 rounded-lg border bg-surface p-4" data-pay-metric={m.key}>
          <p className="text-xs text-muted-foreground">{t(`metric.${m.key.replace(".", "_") as "touchless_rate"}`)}</p>
          <p className="figure text-2xl leading-tight">
            <PayMetricValue metric={m} />
          </p>
          <PayMetricRecords metric={m} />
        </li>
      ))}
    </ul>
  );
}

/**
 * One Pay role's dashboard (brief 5.1): what needs the person now, then the rest, the
 * amounts and the numbers. Every count, amount and record links to its page.
 */
export function PayRoleDashboard({ dashboard, data }: { dashboard: PayDashboardKey; data: PayDashboard }) {
  const t = useTranslations("PayDashboards");
  const action = data.action.filter((s) => SECTIONS.has(s.key));
  const waiting = action.reduce((n, s) => n + s.count, 0);
  return (
    <section aria-labelledby={`pay-dash-${dashboard}`} className="space-y-5" data-pay-dashboard={dashboard}>
      <header className="space-y-1">
        <h2 id={`pay-dash-${dashboard}`} className="font-display text-[1.75rem] leading-tight">
          {t(`title.${dashboard}`)}
        </h2>
      </header>
      <div className="space-y-3">
        <h3 className="eyebrow">{t("needsYou", { count: waiting })}</h3>
        <div className="grid gap-3 md:grid-cols-2">
          {action.map((s) => (
            <SectionCard key={s.key} board={dashboard} section={s} prominent />
          ))}
        </div>
      </div>
      <Amounts amounts={data.amounts} />
      {data.sections.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2">
          {data.sections.map((s) => (
            <SectionCard key={s.key} board={dashboard} section={s} />
          ))}
        </div>
      )}
      {data.metrics.length > 0 && (
        <div className="space-y-2">
          <h3 className="eyebrow">{t("howItsGoing")}</h3>
          <Metrics metrics={data.metrics} />
        </div>
      )}
      {data.links.length > 0 && (
        <nav aria-label={t("pages")} className="flex flex-wrap gap-3 text-sm">
          {data.links
            .flatMap((l) => (PAY_PAGE_LINKS[l] ? [{ l, href: PAY_PAGE_LINKS[l] }] : []))
            .map(({ l, href }) => (
              <Link key={l} href={href} className="inline-flex items-center gap-1 text-primary hover:underline" data-pay-link={l}>
                {t(`link.${l as "inbox"}`)}
                <ArrowUpRight className="size-3 rtl:-scale-x-100" aria-hidden />
              </Link>
            ))}
        </nav>
      )}
    </section>
  );
}
