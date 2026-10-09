// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { JobList } from "@/components/careers/job-parts";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { fetchCareers } from "@/lib/careers/server";
import { serverEnv } from "@/lib/env";

interface Props {
  params: Promise<{ locale: string; orgSlug: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { locale: raw, orgSlug } = await params;
  const locale = toLocale(raw);
  const careers = await fetchCareers(orgSlug, locale).catch(() => null);
  if (!careers) return { robots: { index: false } };
  const t = await getTranslations({ locale, namespace: "Careers" });
  const base = serverEnv().baseUrl.replace(/\/$/, "");
  const org = careers.organisation.name;
  return {
    title: t("metaTitle", { org }),
    description: t("metaDescription", { org }),
    robots: { index: true, follow: true },
    alternates: {
      canonical: `${base}/${locale}/careers/${orgSlug}`,
      languages: { en: `${base}/en/careers/${orgSlug}`, ar: `${base}/ar/careers/${orgSlug}` },
    },
    openGraph: { title: t("metaTitle", { org }), description: t("metaDescription", { org }), type: "website" },
  };
}

/** `/[locale]/careers/[orgSlug]`: an organisation's open jobs. Public. */
export default async function CareersPage({ params }: Props) {
  const { locale: raw, orgSlug } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  const careers = await fetchCareers(orgSlug, locale);
  if (!careers) notFound();
  const t = await getTranslations({ locale, namespace: "Careers" });
  const tPortal = await getTranslations({ locale, namespace: "Portal" });
  const org = careers.organisation.name;
  return (
    <div className="space-y-10">
      <section className="space-y-4">
        <h1 className="font-display text-4xl leading-tight text-balance sm:text-5xl">{t("heading", { org })}</h1>
        <p className="max-w-prose text-lg leading-relaxed text-muted-foreground">{t("intro")}</p>
      </section>
      <section aria-labelledby="open-roles" className="space-y-4">
        <div className="flex items-baseline justify-between gap-4">
          <h2 id="open-roles" className="font-display text-2xl">
            {t("openRoles")}
          </h2>
          <p className="text-sm text-muted-foreground">{t("roleCount", { count: careers.jobs.length })}</p>
        </div>
        <JobList orgSlug={orgSlug} jobs={careers.jobs} />
      </section>
      <p className="text-sm text-muted-foreground">
        {tPortal("signIn.title")}:{" "}
        <Link href={`/portal?org=${orgSlug}`} className="text-primary underline-offset-4 hover:underline" data-portal-link>
          {tPortal("signIn.send")}
        </Link>
      </p>
    </div>
  );
}
