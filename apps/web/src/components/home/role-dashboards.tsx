// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { ArrowUpRight, CircleAlert, Video } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";

import { MetricValue, Records, SurveySummary } from "@/components/hire/hiring-analytics";
import { Link } from "@/i18n/navigation";
import {
  anchor,
  itemHref,
  PAGE_LINKS,
  sectionHref,
  type Dashboard,
  type DashboardKey,
  type DashItem,
  type DashMetric,
  type DashSection,
} from "@/lib/hire/dashboards";

/** A section's key as a message key (the API's keys are fixed, see `dashboards.py`). */
type SectionKey =
  | "to_review"
  | "flags"
  | "approvals"
  | "waiting"
  | "missing_scorecards"
  | "sla"
  | "interviews_today"
  | "interviews_week"
  | "jobs"
  | "shortlists"
  | "debriefs"
  | "offers"
  | "requisitions_open"
  | "requisitions"
  | "scorecards_due"
  | "interviews_next"
  | "interviews_upcoming"
  | "interviews_past"
  | "load_week"
  | "approvals_late"
  | "referrals_active"
  | "referrals"
  | "open_jobs";

const SECTION_KEYS = new Set<string>([
  "to_review",
  "flags",
  "approvals",
  "waiting",
  "missing_scorecards",
  "sla",
  "interviews_today",
  "interviews_week",
  "jobs",
  "shortlists",
  "debriefs",
  "offers",
  "requisitions_open",
  "requisitions",
  "scorecards_due",
  "interviews_next",
  "interviews_upcoming",
  "interviews_past",
  "load_week",
  "approvals_late",
  "referrals_active",
  "referrals",
  "open_jobs",
]);

const REQUISITION_STATUSES = ["pending_approval", "approved", "rejected", "changes_requested"] as const;
function requisitionStatus(value: string): (typeof REQUISITION_STATUSES)[number] | "other" {
  return (REQUISITION_STATUSES as readonly string[]).includes(value) ? (value as (typeof REQUISITION_STATUSES)[number]) : "other";
}

function approvalType(value: string): "offer" | "requisition" | "message" | "other" {
  if (value === "hire.offer") return "offer";
  if (value === "hire.requisition") return "requisition";
  if (value === "hire.message") return "message";
  return "other";
}

function flagKind(value: string): "injection" | "claims" | "ai_signals" | "other" {
  return value === "injection" || value === "claims" || value === "ai_signals" ? value : "other";
}

function isLocal(href: string): boolean {
  return href.startsWith("#");
}

/** A link inside the app, or an in-page anchor to the list below. */
function To({ href, className, children, ...rest }: { href: string; className?: string; children: React.ReactNode } & Record<string, unknown>) {
  if (isLocal(href)) {
    return (
      <a href={href} className={className} {...rest}>
        {children}
      </a>
    );
  }
  return (
    <Link href={href} className={className} {...rest}>
      {children}
    </Link>
  );
}

function ItemLine({ item }: { item: DashItem }) {
  const t = useTranslations("Dashboards");
  const format = useFormatter();
  const href = itemHref(item);
  const d = item.detail;
  const bits: string[] = [];
  if (item.kind === "interview" && item.at) bits.push(format.dateTime(new Date(item.at), { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }));
  if (typeof d.waiting_days === "number") bits.push(t("detail.waiting", { days: d.waiting_days }));
  if (typeof d.interviewer === "string" && d.interviewer) bits.push(d.interviewer);
  if (typeof d.job_title === "string") bits.push(d.job_title);
  if (typeof d.stage_label === "string") bits.push(d.stage_label);
  if (typeof d.count === "number") bits.push(t("detail.candidates", { count: d.count }));
  if (typeof d.reference === "string" && item.kind === "requisition") bits.push(d.reference);
  if (item.kind === "requisition" && typeof d.status === "string") bits.push(t(`requisitionStatus.${requisitionStatus(d.status)}`));
  if (item.kind === "approval" && typeof d.type === "string") bits.push(t(`approvalType.${approvalType(d.type)}`));
  if (typeof d.flags === "string" && d.flags) bits.push(d.flags.split(",").map((f) => t(`flag.${flagKind(f)}`)).join(", "));
  if (d.submitted === true) bits.push(t("detail.submitted"));
  if (d.submitted === false) bits.push(t("detail.notSubmitted"));
  const label = item.kind === "approval" ? t("detail.approval") : item.label;
  const overdue = d.sla_overdue === true || d.sla === "overdue";
  const join = typeof d.meeting_url === "string" && d.meeting_url.startsWith("https://") ? d.meeting_url : null;
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 py-2 text-sm" data-dash-item={item.kind}>
      <span className="min-w-0 space-y-0.5">
        {href ? (
          <Link href={href} className="font-medium hover:text-primary hover:underline" dir="auto">
            {label}
          </Link>
        ) : (
          <span className="font-medium" dir="auto">
            {label}
          </span>
        )}
        {bits.length > 0 && <span className="block text-xs text-muted-foreground">{bits.join(" · ")}</span>}
      </span>
      <span className="flex items-center gap-2">
        {overdue && (
          <span className="inline-flex items-center gap-1 rounded-full bg-signal-soft px-2 py-0.5 text-[0.7rem] text-signal-foreground dark:text-signal">
            <CircleAlert className="size-3" aria-hidden />
            {t("detail.overdue")}
          </span>
        )}
        {join && (
          <a href={join} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
            <Video className="size-3.5" aria-hidden />
            {t("detail.join")}
          </a>
        )}
      </span>
    </li>
  );
}

function JobLine({ item }: { item: DashItem }) {
  const t = useTranslations("Dashboards");
  const href = itemHref(item);
  return (
    <li className="space-y-1.5 py-2.5" data-dash-item="job">
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
        {href ? (
          <Link href={href} className="font-medium hover:text-primary hover:underline" dir="auto">
            {item.label}
          </Link>
        ) : (
          <span className="font-medium">{item.label}</span>
        )}
        {typeof item.detail.team === "string" && <span className="text-xs text-muted-foreground">{item.detail.team}</span>}
      </div>
      {item.stages.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label={t("pipeline")}>
          {item.stages.map((s) => (
            <li key={s.key}>
              <Link
                href={`/hire/jobs/${item.job_id}/pipeline`}
                className="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs hover:text-primary"
                data-stage={s.key}
              >
                <StageName label={s.label} labelAr={s.label_ar} />
                <span className="font-mono">{s.count}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function StageName({ label, labelAr }: { label: string; labelAr: string }) {
  const locale = useLocale();
  return <span>{locale === "ar" && labelAr ? labelAr : label}</span>;
}

function SectionCard({ dashboard, section, prominent }: { dashboard: DashboardKey; section: DashSection; prominent?: boolean }) {
  const t = useTranslations("Dashboards");
  if (!SECTION_KEYS.has(section.key)) return null;
  const key = section.key as SectionKey;
  const id = anchor(dashboard, section.key);
  const href = sectionHref(dashboard, section);
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-24 space-y-2 rounded-xl border bg-surface p-4 shadow-soft" data-dash-section={section.key}>
      <header className="flex items-baseline justify-between gap-2">
        <h3 id={`${id}-title`} className="text-sm font-medium">
          {t(`section.${key}`)}
        </h3>
        <To
          href={href}
          className={`font-display ${prominent ? "text-2xl" : "text-xl"} hover:text-primary hover:underline`}
          aria-label={t("countLink", { count: section.count, what: t(`section.${key}`) })}
          data-dash-count={section.count}
        >
          {section.count}
        </To>
      </header>
      {section.items.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t(`empty.${prominent ? "action" : "section"}`)}</p>
      ) : (
        <ul className="divide-y">
          {section.items.map((item) =>
            item.kind === "job" && item.stages.length > 0 ? <JobLine key={item.id} item={item} /> : <ItemLine key={`${item.kind}:${item.id}`} item={item} />,
          )}
        </ul>
      )}
      {section.count > section.items.length && (
        <p className="text-xs">
          <To href={isLocal(href) ? "/hire" : href} className="inline-flex items-center gap-1 text-primary hover:underline">
            {t("seeAll", { count: section.count })}
            <ArrowUpRight className="size-3 rtl:-scale-x-100" aria-hidden />
          </To>
        </p>
      )}
    </section>
  );
}

function MetricTitle({ metric: m }: { metric: DashMetric }) {
  const t = useTranslations("Dashboards");
  return <>{metricTitle(t, m)}</>;
}

function metricTitle(t: ReturnType<typeof useTranslations<"Dashboards">>, m: DashMetric): string {
  const [head, rest] = [m.key.split(".")[0], m.key.split(".").slice(1).join(".")];
  const d = m.detail as Record<string, unknown>;
  switch (head) {
    case "time_to_first_response":
      return t("metric.firstResponse");
    case "time_in_stage":
      return t("metric.timeInStage", { stage: rest });
    case "time_to_hire":
      return t("metric.timeToHire");
    case "stage_conversion":
      return t("metric.conversion", { from: String(d.stage ?? rest), to: String(d.next_stage ?? "") });
    case "source":
      return t("metric.source", { source: rest });
    case "ghosting_rate":
      return t("metric.ghosting");
    case "interviewer_load":
      return t("metric.interviewerLoad", { name: String(d.name || t("metric.someone")) });
    case "ai_human_agreement":
      return t("metric.agreement");
    case "ai_human_disagreement":
      return t("metric.disagreement");
    case "override_rate":
      return t("metric.overrides");
    case "flags":
      return t(`flag.${flagKind(rest)}`);
    default:
      return m.key;
  }
}

function Metrics({ metrics }: { metrics: DashMetric[] }) {
  const t = useTranslations("Dashboards");
  if (metrics.length === 0) return null;
  return (
    <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {metrics.map((m) => (
        <li key={m.key} className="space-y-1 rounded-lg border bg-surface p-3" data-metric={m.key}>
          <p className="text-xs text-muted-foreground" dir="auto">
            <MetricTitle metric={m} />
          </p>
          <p className="font-display text-xl">
            <MetricValue metric={m} />
          </p>
          <p className="text-xs text-muted-foreground">{t("sample", { n: m.n })}</p>
          <Records metric={m} />
        </li>
      ))}
    </ul>
  );
}

/**
 * One role's dashboard (brief 5.1): "what needs me now" first, then the rest, then the
 * numbers. Every count links to its records (the page that holds them, or the list
 * right below it), and every record to its own page.
 */
export function RoleDashboard({ dashboard, data }: { dashboard: DashboardKey; data: Dashboard }) {
  const t = useTranslations("Dashboards");
  const action = data.action.filter((s) => SECTION_KEYS.has(s.key));
  const waiting = action.reduce((n, s) => n + s.count, 0);
  return (
    <section aria-labelledby={`dash-${dashboard}`} className="space-y-5" data-dashboard={dashboard}>
      <header className="space-y-1">
        <h2 id={`dash-${dashboard}`} className="font-display text-2xl">
          {t(`title.${dashboard}`)}
        </h2>
        <p className="text-sm text-muted-foreground">
          {t(`scope.${data.scope === "mine" ? "mine" : "all"}`)}
        </p>
      </header>
      <div className="space-y-3">
        <h3 className="eyebrow">{t("needsYou", { count: waiting })}</h3>
        <div className="grid gap-3 md:grid-cols-2">
          {action.map((s) => (
            <SectionCard key={s.key} dashboard={dashboard} section={s} prominent />
          ))}
        </div>
      </div>
      {data.sections.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2">
          {data.sections.map((s) => (
            <SectionCard key={s.key} dashboard={dashboard} section={s} />
          ))}
        </div>
      )}
      {data.metrics.length > 0 && (
        <div className="space-y-2">
          <h3 className="eyebrow">{t("howItsGoing")}</h3>
          <Metrics metrics={data.metrics} />
        </div>
      )}
      {data.survey && <SurveySummary survey={data.survey} />}
      {data.links.length > 0 && (
        <nav aria-label={t("pages")} className="flex flex-wrap gap-3 text-sm">
          {data.links
            .flatMap((l) => (PAGE_LINKS[l] ? [{ l, href: PAGE_LINKS[l] }] : []))
            .map(({ l, href }) => (
              <Link key={l} href={href} className="inline-flex items-center gap-1 text-primary hover:underline" data-dash-link={l}>
                {t(`link.${l as "analytics"}`)}
                <ArrowUpRight className="size-3 rtl:-scale-x-100" aria-hidden />
              </Link>
            ))}
        </nav>
      )}
    </section>
  );
}
