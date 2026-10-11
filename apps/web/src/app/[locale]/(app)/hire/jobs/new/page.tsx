// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { JobEditor } from "@/components/hire/job-editor";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { PageHeader } from "@/components/states/page-header";
import { Unavailable } from "@/components/states/unavailable";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { getSession, twoFactorEnabled } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "HireJob" });
  return { title: t("newTitle") };
}

/** A new job from a short brief (brief 5.3). The rubric follows once the job is saved. */
export default async function NewJobPage({ params }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 py-10 sm:px-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-4xl px-5 py-10 sm:px-8">
        <Unavailable retryHref={`/${locale}/hire/jobs/new`} />
      </div>
    );
  }
  const permissions = viewer.me.permissions;
  if (!permissions.includes("hire.job.write")) notFound();
  const t = await getTranslations("HireJob");
  return (
    <div className="mx-auto w-full max-w-4xl space-y-8 px-5 py-10 sm:px-8">
      <Link href="/hire" className="text-sm text-primary hover:underline">
        {t("back")}
      </Link>
      <PageHeader title={t("newTitle")}>
        <p>{t("newIntro")}</p>
      </PageHeader>
      <JobEditor
        canRubric={permissions.includes("hire.rubric.write")}
        canLock={permissions.includes("hire.rubric.lock")}
        canPublish={permissions.includes("hire.job.publish")}
        totpEnabled={twoFactorEnabled(session)}
      />
    </div>
  );
}
