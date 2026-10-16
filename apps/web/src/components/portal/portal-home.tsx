// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowRight, Briefcase, FileUp } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Link } from "@/i18n/navigation";
import { loadPortalApplications } from "@/lib/actions/portal";
import { fingerprint, type PortalApplication, type PortalJob, type PortalMe } from "@/lib/portal/steps";

import { StatusTimeline } from "./status-timeline";

export const POLL_MS = 20_000;

/**
 * Keeps applications current while the page is open (brief 3.9.1, live status): asks the
 * API again every 20 seconds while the tab is visible and announces a change politely.
 */
export function useLiveApplications(
  initial: PortalApplication[],
  load: typeof loadPortalApplications = loadPortalApplications,
): { apps: PortalApplication[]; changed: boolean } {
  const [apps, setApps] = useState(initial);
  const [changed, setChanged] = useState(false);
  const seen = useRef(fingerprint(initial));
  useEffect(() => {
    const tick = async () => {
      if (document.visibilityState !== "visible") return;
      const result = await load();
      if (!result.ok) return;
      const next = fingerprint(result.applications);
      if (next !== seen.current) {
        seen.current = next;
        setApps(result.applications);
        setChanged(true);
      }
    };
    const timer = window.setInterval(() => void tick(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);
  return { apps, changed };
}

/**
 * The candidate portal's home: every application with where it stands, what the team
 * asked for and upcoming interviews, then the organisation's other open jobs. Mobile
 * first, one column.
 */
export function PortalHome({
  me,
  applications,
  jobs,
  load = loadPortalApplications,
}: {
  me: PortalMe;
  applications: PortalApplication[];
  jobs: PortalJob[];
  load?: typeof loadPortalApplications;
}) {
  const t = useTranslations("Portal");
  const format = useFormatter();
  const locale = useLocale();
  const { apps, changed } = useLiveApplications(applications, load);
  const others = jobs.filter((j) => !j.applied);

  return (
    <div className="space-y-10" data-portal-home>
      <header className="space-y-2">
        <p className="eyebrow">{me.organisation}</p>
        <h1 className="font-display text-3xl leading-tight sm:text-4xl">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("app.live")}</p>
        <p role="status" aria-live="polite" className="text-sm font-medium" data-live-status>
          {changed ? t("app.updated") : ""}
        </p>
      </header>

      <ul className="space-y-6">
        {apps.map((app) => (
          <li key={app.id}>
            <article
              aria-labelledby={`app-${app.id}`}
              className="space-y-4 rounded-2xl border bg-surface p-5 shadow-soft"
              data-portal-application={app.id}
              data-status={app.status}
            >
              <div className="space-y-1">
                <h2 id={`app-${app.id}`} className="font-display text-xl leading-snug">
                  {locale === "ar" && app.job_title_ar ? app.job_title_ar : app.job_title}
                </h2>
                <p className="text-xs text-muted-foreground">
                  <span dir="ltr">{t("app.reference", { reference: app.reference })}</span>
                  {" · "}
                  {t("app.appliedOn", { date: format.dateTime(new Date(app.submitted_at), { dateStyle: "medium" }) })}
                </p>
              </div>
              <StatusTimeline app={app} />
              <div className="flex flex-wrap items-center gap-2">
                {app.open_document_requests > 0 && (
                  <Badge variant="signal">
                    <FileUp className="size-3" aria-hidden />
                    {t("app.filesWanted", { count: app.open_document_requests })}
                  </Badge>
                )}
                {app.interviews.length > 0 && <Badge>{t("app.interviews")}</Badge>}
                <Link
                  href={`/portal/applications/${app.id}`}
                  className="ms-auto inline-flex items-center gap-1 text-sm font-medium text-primary underline-offset-4 hover:underline"
                  data-open-application={app.id}
                >
                  {t("app.open")}
                  <ArrowRight className="size-4 rtl:rotate-180" aria-hidden />
                </Link>
              </div>
            </article>
          </li>
        ))}
      </ul>

      <section aria-labelledby="other-jobs" className="space-y-3">
        <h2 id="other-jobs" className="flex items-center gap-2 font-display text-2xl">
          <Briefcase className="size-5" aria-hidden />
          {t("jobs.title")}
        </h2>
        {others.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("jobs.none")}</p>
        ) : (
          <ul className="divide-y rounded-xl border bg-surface">
            {others.map((job) => (
              <li key={job.slug} className="flex items-center justify-between gap-3 px-4 py-3">
                <span className="min-w-0">
                  <span className="block truncate font-medium">
                    {locale === "ar" && job.title_ar ? job.title_ar : job.title}
                  </span>
                  <span className="block truncate text-xs text-muted-foreground">{job.location}</span>
                </span>
                <Link
                  href={`/careers/${me.org_slug}/${job.slug}`}
                  className="shrink-0 text-sm font-medium text-primary underline-offset-4 hover:underline"
                >
                  {t("jobs.apply")}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
