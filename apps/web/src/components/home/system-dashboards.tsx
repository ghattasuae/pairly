// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { ArrowUpRight } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type * as React from "react";

import { Link } from "@/i18n/navigation";
import {
  detailEntries,
  formatBytes,
  safeHref,
  statValue,
  type SysItem,
  type SysSection,
  type SysStat,
  type SystemDashboard,
  type SystemDashboardKey,
} from "@/lib/system-dashboards";

type T = ReturnType<typeof useTranslations<"SystemDashboards">>;

/** A translated label for a key the API sends, else the key itself (plugin sections and
 * per-workflow stats add keys over time; an unknown one still reads). */
function label(t: T, group: string, key: string): string {
  const path = `${group}.${key}`;
  return t.has(path as "title.auditor") ? t(path as "title.auditor") : key.replace(/_/g, " ");
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

function StatLine({ stat }: { stat: SysStat }) {
  const t = useTranslations("SystemDashboards");
  const format = useFormatter();
  const value = statValue(stat);
  let shown: React.ReactNode;
  if (value.kind === "none") {
    shown = (
      <span className="text-muted-foreground" data-no-data>
        {t("noData")}
      </span>
    );
  } else if (value.kind === "text") {
    shown = label(t, "value", value.text);
  } else if (value.unit === "ratio") {
    shown = format.number(value.value, { style: "percent", maximumFractionDigits: 1 });
  } else if (value.unit === "usd") {
    shown = format.number(value.value, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
  } else if (value.unit === "ms") {
    shown = t("unit.ms", { value: format.number(value.value, { maximumFractionDigits: 1 }) });
  } else if (value.unit === "seconds") {
    shown = t("unit.seconds", { value: format.number(value.value, { maximumFractionDigits: 1 }) });
  } else if (value.unit === "bytes") {
    shown = <span dir="ltr">{formatBytes(value.value)}</span>;
  } else {
    shown = format.number(value.value, { maximumFractionDigits: 2 });
  }
  return (
    <li className="flex items-baseline justify-between gap-3 text-sm" data-sys-stat={stat.key}>
      <span className="text-muted-foreground">{label(t, "stat", stat.key)}</span>
      <To href={stat.href} className="font-mono tabular-nums hover:text-primary hover:underline">
        {shown}
        {stat.n !== null && stat.n !== undefined && value.kind === "number" && value.unit === "ratio" && (
          <span className="ms-1 text-xs text-muted-foreground">{t("ofN", { count: stat.n })}</span>
        )}
      </To>
    </li>
  );
}

function ItemLine({ item }: { item: SysItem }) {
  const t = useTranslations("SystemDashboards");
  const format = useFormatter();
  const bits = detailEntries(item.detail).map(([key, value]) => {
    const name = label(t, "detail", key);
    const text =
      typeof value === "boolean" ? t(value ? "yes" : "no") : typeof value === "number" ? format.number(value, { maximumFractionDigits: 4 }) : value;
    return `${name}: ${text}`;
  });
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-0.5 py-1.5 text-sm" data-sys-item={item.kind}>
      <To href={item.href} className="min-w-0 font-medium [overflow-wrap:anywhere] hover:text-primary hover:underline" dir="auto">
        {item.label}
      </To>
      {item.status && (
        <span className="rounded border px-1.5 text-xs" data-status={item.status}>
          {label(t, "status", item.status)}
        </span>
      )}
      {item.at && (
        <time className="text-xs text-muted-foreground" dateTime={item.at}>
          {format.dateTime(new Date(item.at), { dateStyle: "medium", timeStyle: "short" })}
        </time>
      )}
      {bits.length > 0 && (
        <span className="w-full text-xs text-muted-foreground [overflow-wrap:anywhere]" dir="auto">
          {bits.join(" · ")}
        </span>
      )}
    </li>
  );
}

function SectionCard({ board, section, prominent }: { board: SystemDashboardKey; section: SysSection; prominent?: boolean }) {
  const t = useTranslations("SystemDashboards");
  const id = `sys-dash-${board}-${section.key}`.replace(/_/g, "-");
  const title = label(t, "section", section.key);
  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className="min-w-0 scroll-mt-24 space-y-2 rounded-xl border bg-surface p-4 shadow-soft"
      data-sys-section={section.key}
    >
      <header className="flex items-baseline justify-between gap-2">
        <h3 id={`${id}-title`} className="text-sm font-medium">
          {title}
        </h3>
        {section.count === null ? (
          <span className="text-xs text-muted-foreground" data-sys-count="none">
            {t("noData")}
          </span>
        ) : (
          <To
            href={section.href}
            className={`font-display ${prominent ? "text-2xl" : "text-xl"} hover:text-primary hover:underline`}
            aria-label={t("countLink", { count: section.count, what: title })}
            data-sys-count={section.count}
          >
            {section.count}
          </To>
        )}
      </header>
      {section.note && (
        <p className="text-xs text-muted-foreground" data-sys-note={section.note}>
          {label(t, "note", section.note)}
        </p>
      )}
      {section.stats.length > 0 && <ul className="space-y-1">{section.stats.map((s) => <StatLine key={s.key} stat={s} />)}</ul>}
      {section.items.length > 0 ? (
        <ul className="divide-y">
          {section.items.map((item) => (
            <ItemLine key={`${item.kind}:${item.id}`} item={item} />
          ))}
        </ul>
      ) : (
        !section.note && section.stats.length === 0 && <p className="text-xs text-muted-foreground">{t(prominent ? "emptyAction" : "emptySection")}</p>
      )}
      {section.source && (
        <p className="text-[11px] text-muted-foreground" data-sys-source={section.source}>
          {t("source", { source: label(t, "sourceName", section.source) })}
        </p>
      )}
      {section.count !== null && section.count > section.items.length && safeHref(section.href) && (
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

/**
 * One shared or system board (brief 5.1 items 12 to 15): what needs the person now, then
 * the rest. Every count links to its records; a number without data says so.
 */
export function SystemRoleDashboard({ dashboard, data }: { dashboard: SystemDashboardKey; data: SystemDashboard }) {
  const t = useTranslations("SystemDashboards");
  const waiting = data.action.reduce((n, s) => n + (s.count ?? 0), 0);
  return (
    <section aria-labelledby={`sys-dash-${dashboard}`} className="space-y-5" data-sys-dashboard={dashboard}>
      <header className="space-y-1">
        <h2 id={`sys-dash-${dashboard}`} className="font-display text-2xl">
          {t(`title.${dashboard}`)}
        </h2>
        <p className="text-sm text-muted-foreground">{t(`intro.${dashboard}`)}</p>
      </header>
      <div className="space-y-3">
        <h3 className="eyebrow">{t("needsYou", { count: waiting })}</h3>
        {data.action.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("nothingNeedsYou")}</p>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {data.action.map((s) => (
              <SectionCard key={s.key} board={dashboard} section={s} prominent />
            ))}
          </div>
        )}
      </div>
      {data.sections.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2">
          {data.sections.map((s) => (
            <SectionCard key={s.key} board={dashboard} section={s} />
          ))}
        </div>
      )}
      {data.links.length > 0 && (
        <nav aria-label={t("pages")} className="flex flex-wrap gap-3 text-sm">
          {data.links.map((l) =>
            safeHref(l.href) ? (
              <Link key={l.key} href={l.href} className="inline-flex items-center gap-1 text-primary hover:underline" data-sys-link={l.key}>
                {label(t, "link", l.key)}
                <ArrowUpRight className="size-3 rtl:-scale-x-100" aria-hidden />
              </Link>
            ) : (
              // A path in the repository (model cards), not a page of the app.
              <span key={l.key} className="text-muted-foreground" data-sys-link={l.key}>
                {label(t, "link", l.key)}: <code dir="ltr">{l.href}</code>
              </span>
            ),
          )}
        </nav>
      )}
    </section>
  );
}
