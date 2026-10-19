// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { getFormatter, getTranslations, setRequestLocale } from "next-intl/server";
import type * as React from "react";

import { JobDescription, JobFacts } from "@/components/careers/job-parts";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { jsonLdScript, localDescription, localTitle } from "@/lib/careers/format";
import { fetchJob } from "@/lib/careers/server";
import { isReferralToken } from "@/lib/careers/apply";
import { serverEnv } from "@/lib/env";

interface Props {
  params: Promise<{ locale: string; orgSlug: string; jobSlug: string }>;
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { locale: raw, orgSlug, jobSlug } = await params;
  const locale = toLocale(raw);
  const job = await fetchJob(orgSlug, jobSlug, locale).catch(() => null);
  if (!job) return { robots: { index: false } };
  const t = await getTranslations({ locale, namespace: "Careers" });
  const title = localTitle(job, locale);
  const base = serverEnv().baseUrl.replace(/\/$/, "");
  const path = `careers/${orgSlug}/${jobSlug}`;
  const description = t("jobMetaDescription", { title, org: job.organisation.name, location: job.location });
  return {
    title: `${title} · ${job.organisation.name}`,
    description,
    robots: { index: true, follow: true },
    alternates: {
      canonical: `${base}/${locale}/${path}`,
      languages: { en: `${base}/en/${path}`, ar: `${base}/ar/${path}` },
    },
    openGraph: { title, description, type: "website" },
  };
}

/** `/[locale]/careers/[orgSlug]/[jobSlug]`: one open job, with JobPosting JSON-LD. Public. */
export default async function JobPage({ params, searchParams }: Props) {
  const { locale: raw, orgSlug, jobSlug } = await params;
  // An employee's referral link: carried to the apply form (the API checks it).
  const ref = (await searchParams)?.ref;
  const referral = typeof ref === "string" && isReferralToken(ref) ? ref : null;
  const applyHref = `/careers/${orgSlug}/${jobSlug}/apply${referral ? `?ref=${referral}` : ""}`;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  const job = await fetchJob(orgSlug, jobSlug, locale);
  if (!job) notFound();
  const t = await getTranslations({ locale, namespace: "Careers" });
  const format = await getFormatter({ locale });
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  const title = localTitle(job, locale);
  return (
    <article className="space-y-10">
      <script
        type="application/ld+json"
        nonce={nonce}
        // Escaped by jsonLdScript: no value can close the element.
        dangerouslySetInnerHTML={{ __html: jsonLdScript(job.json_ld) }}
      />
      <nav>
        <Link
          href={`/careers/${orgSlug}`}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-primary underline-offset-4 hover:underline"
        >
          <ArrowLeft className="size-3.5 rtl:-scale-x-100" aria-hidden />
          {t("allRoles", { org: job.organisation.name })}
        </Link>
      </nav>
      <header className="rise max-w-4xl space-y-5 border-b border-border pb-10">
        <p className="eyebrow">{job.organisation.name}</p>
        <h1 className="font-display text-5xl leading-[1.05] font-normal text-balance sm:text-6xl">{title}</h1>
        <p className="font-mono text-xs text-muted-foreground">
          {t("posted", { date: format.dateTime(new Date(job.published_at), { dateStyle: "long" }) })}
        </p>
      </header>
      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_20rem] lg:gap-16">
        {/* First in reading order (phones see the facts and the button first); beside the
            text, sticky, from lg. */}
        <div className="lg:sticky lg:top-8 lg:col-start-2 lg:row-start-1 lg:self-start">
          <section aria-label={t("facts")} className="rise space-y-6 rounded-lg border border-border bg-surface p-6" style={{ "--rise-index": 1 } as React.CSSProperties}>
            <p className="eyebrow">{t("facts")}</p>
            <JobFacts job={job} className="grid gap-3 text-sm" />
            <Button asChild size="lg" className="min-h-11 w-full">
              <Link href={applyHref}>{t("applyCta")}</Link>
            </Button>
          </section>
        </div>
        <div className="max-w-prose space-y-10 lg:col-start-1 lg:row-start-1">
          <JobDescription text={localDescription(job, locale)} />
          <div className="border-t border-border pt-8">
            <Button asChild size="lg" className="min-h-11">
              <Link href={applyHref}>{t("applyCta")}</Link>
            </Button>
          </div>
        </div>
      </div>
    </article>
  );
}
