// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Clock, Copy, Loader2, Search, Share2, UsersRound } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Link } from "@/i18n/navigation";
import { errorKey } from "@/lib/action-result";
import { referralLink, rediscover } from "@/lib/actions/hire";
import type { HireJob, HireRediscoveryHit, HireReferral, HireWaitingList } from "@/lib/hire/types";

/** "Candidates waiting longest" (brief 3.5.3): the anti-ghosting card, action first. */
export function WaitingCard({ waiting }: { waiting: HireWaitingList }) {
  const t = useTranslations("Hire");
  const ids = useId();
  return (
    <section aria-labelledby={ids} className="space-y-3 rounded-xl border bg-surface p-5" data-waiting-card>
      <div className="flex items-baseline justify-between gap-3">
        <h2 id={ids} className="font-display text-xl">
          {t("home.waitingTitle")}
        </h2>
        <span className="text-xs text-muted-foreground">{t("home.slaDays", { days: waiting.sla_days })}</span>
      </div>
      {waiting.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("home.nobodyWaiting")}</p>
      ) : (
        <ol className="divide-y">
          {waiting.items.map((item) => (
            <li key={item.application_id}>
              <Link
                href={`/hire/jobs/${item.job_id}/pipeline?candidate=${item.application_id}`}
                className="flex items-center justify-between gap-3 py-2.5 text-sm hover:text-primary"
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">{item.label}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {item.job_title} · {item.stage_label}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  {item.has_open_draft && <Badge variant="outline">{t("home.drafted")}</Badge>}
                  <Badge variant={item.sla_overdue ? "signal" : "outline"}>
                    <Clock className="size-3" aria-hidden />
                    {t("card.waiting", { days: item.waiting_days })}
                  </Badge>
                </span>
              </Link>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** Find a past candidate by skill (talent rediscovery): one search. */
export function RediscoverySearch() {
  const t = useTranslations("Hire");
  const tErrors = useTranslations("Errors");
  const ids = useId();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<HireRediscoveryHit[] | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    setPending(true);
    setError(null);
    const result = await rediscover(query);
    setPending(false);
    if (result.ok) setHits(result.hits);
    else setError(result.code);
  };

  return (
    <section aria-labelledby={ids} className="space-y-3 rounded-xl border bg-surface p-5">
      <div className="space-y-1">
        <h2 id={ids} className="font-display text-xl">
          {t("home.rediscoverTitle")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("home.rediscoverBody")}</p>
      </div>
      <form onSubmit={run} className="flex gap-2" role="search">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("home.rediscoverPlaceholder")}
          aria-label={t("home.rediscoverPlaceholder")}
          maxLength={300}
        />
        <Button type="submit" disabled={pending}>
          {pending ? <Loader2 className="animate-spin" aria-hidden /> : <Search aria-hidden />}
          {t("home.search")}
        </Button>
      </form>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {tErrors(errorKey(error) as "generic")}
        </p>
      )}
      {hits !== null &&
        (hits.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("home.noMatches")}</p>
        ) : (
          <ul className="divide-y" data-rediscovery-hits>
            {hits.map((hit) => {
              const latest = hit.applications[0] as Record<string, string> | undefined;
              return (
                <li key={hit.candidate_id} className="space-y-1 py-2.5 text-sm">
                  <p className="font-medium">{hit.label}</p>
                  {latest && (
                    <Link
                      href={`/hire/jobs/${latest.job_id}/pipeline?candidate=${latest.application_id}`}
                      className="text-xs text-primary hover:underline"
                    >
                      {latest.job_title} · {latest.stage_label}
                    </Link>
                  )}
                  <p className="line-clamp-2 text-xs text-muted-foreground">{hit.snippet}</p>
                </li>
              );
            })}
          </ul>
        ))}
    </section>
  );
}

/** Refer someone: a personal link per open job, and the stage of the people you referred. */
export function Referrals({ jobs, referrals }: { jobs: HireJob[]; referrals: HireReferral[] }) {
  const t = useTranslations("Hire");
  const format = useFormatter();
  const locale = useLocale() as "en" | "ar";
  const ids = useId();
  const [copied, setCopied] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const copy = async (jobId: string) => {
    setError(null);
    const result = await referralLink(jobId, locale);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    try {
      await navigator.clipboard.writeText(result.url);
      setCopied(jobId);
    } catch {
      setCopied(null);
      window.prompt(t("home.copyManually"), result.url);
    }
  };

  return (
    <section aria-labelledby={ids} className="space-y-4 rounded-xl border bg-surface p-5">
      <div className="space-y-1">
        <h2 id={ids} className="font-display text-xl">
          {t("home.referTitle")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("home.referBody")}</p>
      </div>
      {jobs.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("home.noOpenJobs")}</p>
      ) : (
        <ul className="divide-y">
          {jobs.map((job) => (
            <li key={job.id} className="flex items-center justify-between gap-3 py-2 text-sm">
              <span className="truncate">{locale === "ar" && job.title_ar ? job.title_ar : job.title}</span>
              <Button type="button" size="sm" variant="outline" onClick={() => void copy(job.id)}>
                {copied === job.id ? <Check aria-hidden /> : <Copy aria-hidden />}
                {copied === job.id ? t("home.copied") : t("home.copyLink")}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {t("home.linkFailed")}
        </p>
      )}
      <div className="space-y-2">
        <h3 className="eyebrow">{t("home.myReferrals")}</h3>
        {referrals.length === 0 ? (
          <EmptyState icon={<Share2 aria-hidden />} title={t("home.noReferrals")} className="py-2" />
        ) : (
          <ul className="divide-y" data-my-referrals>
            {referrals.map((r) => (
              <li key={r.referral_id} className="flex items-center justify-between gap-3 py-2 text-sm">
                <span className="min-w-0">
                  <span className="block truncate font-medium">{r.candidate}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {r.job_title} · {format.dateTime(new Date(r.referred_at), { dateStyle: "medium" })}
                  </span>
                </span>
                <Badge variant="outline">{r.stage_label}</Badge>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/** The jobs the hiring team works on, each one click from its pipeline. */
export function JobsList({ jobs }: { jobs: HireJob[] }) {
  const t = useTranslations("Hire");
  const tScreen = useTranslations("Screen");
  const locale = useLocale();
  const ids = useId();
  return (
    <section aria-labelledby={ids} className="space-y-3">
      <h2 id={ids} className="eyebrow">
        {t("home.jobs")}
      </h2>
      {jobs.length === 0 ? (
        <EmptyState icon={<UsersRound aria-hidden />} title={t("home.noJobs")} />
      ) : (
        <ul className="divide-y rounded-xl border bg-surface">
          {jobs.map((job) => (
            <li key={job.id} className="flex items-stretch">
              <Link
                href={`/hire/jobs/${job.id}/pipeline`}
                className="flex min-w-0 flex-1 items-center justify-between gap-3 px-4 py-3 text-sm hover:bg-accent/40"
                data-job-link={job.slug}
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">{locale === "ar" && job.title_ar ? job.title_ar : job.title}</span>
                  <span className="block truncate text-xs text-muted-foreground">{job.location}</span>
                </span>
                <Badge variant={job.status === "open" ? "default" : "outline"}>
                  {t(`jobStatus.${job.status}` as "jobStatus.open")}
                </Badge>
              </Link>
              <Link
                href={`/hire/jobs/${job.id}/screen`}
                className="flex items-center border-s px-4 text-sm text-primary hover:bg-accent/40"
                data-screen-link={job.slug}
              >
                {tScreen("open")}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
