// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { getFormatter, getTranslations, setRequestLocale } from "next-intl/server";

import { JobDescription, JobFacts } from "@/components/careers/job-parts";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { jsonLdScript, localDescription, localTitle } from "@/lib/careers/format";
import { fetchJob } from "@/lib/careers/server";
import { serverEnv } from "@/lib/env";

interface Props {
  params: Promise<{ locale: string; orgSlug: string; jobSlug: string }>;
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
export default async function JobPage({ params }: Props) {
  const { locale: raw, orgSlug, jobSlug } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  const job = await fetchJob(orgSlug, jobSlug, locale);
  if (!job) notFound();
  const t = await getTranslations({ locale, namespace: "Careers" });
  const format = await getFormatter({ locale });
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  const title = localTitle(job, locale);
  return (
    <article className="space-y-8">
      <script
        type="application/ld+json"
        nonce={nonce}
        // Escaped by jsonLdScript: no value can close the element.
        dangerouslySetInnerHTML={{ __html: jsonLdScript(job.json_ld) }}
      />
      <nav>
        <Link href={`/careers/${orgSlug}`} className="text-sm font-medium text-primary underline-offset-4 hover:underline">
          {t("allRoles", { org: job.organisation.name })}
        </Link>
      </nav>
      <header className="space-y-4">
        <p className="text-sm text-muted-foreground">{job.organisation.name}</p>
        <h1 className="font-display text-4xl leading-tight text-balance sm:text-5xl">{title}</h1>
        <p className="text-sm text-muted-foreground">
          {t("posted", { date: format.dateTime(new Date(job.published_at), { dateStyle: "long" }) })}
        </p>
        <section aria-label={t("facts")} className="rounded-xl border border-border bg-surface p-4 shadow-xs">
          <JobFacts job={job} className="grid gap-2 text-sm sm:grid-cols-2" />
        </section>
        <Button asChild size="lg" className="min-h-11">
          <Link href={`/careers/${orgSlug}/${jobSlug}/apply`}>{t("applyCta")}</Link>
        </Button>
      </header>
      <JobDescription text={localDescription(job, locale)} />
      <div className="border-t border-border pt-6">
        <Button asChild size="lg" className="min-h-11">
          <Link href={`/careers/${orgSlug}/${jobSlug}/apply`}>{t("applyCta")}</Link>
        </Button>
      </div>
    </article>
  );
}
