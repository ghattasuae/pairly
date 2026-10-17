// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { ApplyFlow } from "@/components/careers/apply-flow";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { localTitle, readPublicForm } from "@/lib/careers/format";
import { fetchJob } from "@/lib/careers/server";

interface Props {
  params: Promise<{ locale: string; orgSlug: string; jobSlug: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { locale: raw, orgSlug, jobSlug } = await params;
  const locale = toLocale(raw);
  const job = await fetchJob(orgSlug, jobSlug, locale).catch(() => null);
  const t = await getTranslations({ locale, namespace: "Careers" });
  // The form itself is not worth indexing; the job page is.
  return {
    title: job ? t("applyTitle", { title: localTitle(job, locale) }) : undefined,
    robots: { index: false, follow: true },
    referrer: "no-referrer",
  };
}

/** `/[locale]/careers/[orgSlug]/[jobSlug]/apply`: the AI-use notice, then the form. Public. */
export default async function ApplyPage({ params }: Props) {
  const { locale: raw, orgSlug, jobSlug } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  const job = await fetchJob(orgSlug, jobSlug, locale);
  if (!job) notFound();
  const t = await getTranslations({ locale, namespace: "Careers" });
  const notice = job.notice[locale] ?? job.notice.en;
  if (!notice) notFound();
  return (
    <div className="space-y-8">
      <nav>
        <Link
          href={`/careers/${orgSlug}/${jobSlug}`}
          className="text-sm font-medium text-primary underline-offset-4 hover:underline"
        >
          {t("back")}
        </Link>
      </nav>
      <h1 className="font-display text-3xl leading-tight text-balance sm:text-4xl">
        {t("applyTitle", { title: localTitle(job, locale) })}
      </h1>
      <ApplyFlow
        orgSlug={orgSlug}
        orgName={job.organisation.name}
        jobSlug={jobSlug}
        locale={locale}
        notice={notice}
        form={readPublicForm(job.form)}
      />
    </div>
  );
}
