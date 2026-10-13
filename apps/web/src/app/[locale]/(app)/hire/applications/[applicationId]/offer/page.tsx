// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { OfferBuilder } from "@/components/hire/offer-builder";
import { NotProvisioned } from "@/components/states/not-provisioned";
import { Unavailable } from "@/components/states/unavailable";
import { toLocale } from "@/i18n/routing";
import { callApi } from "@/lib/api/call";
import { getSession } from "@/lib/auth/server";
import { resolveViewer } from "@/lib/provisioning";

interface Props {
  params: Promise<{ locale: string; applicationId: string }>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Offers" });
  return { title: t("title") };
}

/** The offer builder for one candidate (brief 3.8.3), for the hiring team. */
export default async function OfferPage({ params }: Props) {
  const { locale: raw, applicationId } = await params;
  const locale = toLocale(raw);
  setRequestLocale(locale);
  if (!UUID_RE.test(applicationId)) notFound();
  const [viewer, session] = await Promise.all([resolveViewer(), getSession()]);
  if (viewer.status === "not_provisioned") {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8">
        <NotProvisioned email={session?.user.email ?? ""} code={viewer.code} />
      </div>
    );
  }
  const retry = `/${locale}/hire/applications/${applicationId}/offer`;
  if (viewer.status !== "ready") {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  if (!viewer.me.permissions.includes("hire.offer.read")) notFound();
  const [offers, templates] = await Promise.all([
    callApi((api) => api.GET("/v1/hire/applications/{application_id}/offers", { params: { path: { application_id: applicationId } } })),
    callApi((api) => api.GET("/v1/hire/offer-templates")),
  ]);
  if (!offers.ok || !templates.ok) {
    if (!offers.ok && offers.code === "not_found") notFound();
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8">
        <Unavailable retryHref={retry} />
      </div>
    );
  }
  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-8">
      <OfferBuilder initial={offers.data} templates={templates.data} defaultTitle="" />
    </div>
  );
}
