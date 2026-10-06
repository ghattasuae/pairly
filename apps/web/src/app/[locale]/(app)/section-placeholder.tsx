// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { Hourglass } from "lucide-react";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { EmptyState } from "@/components/states/empty-state";
import { PageHeader } from "@/components/states/page-header";
import type { Locale } from "@/i18n/routing";
import { isNavVisible, type NavKey } from "@/lib/nav";
import { resolveViewer } from "@/lib/provisioning";

/**
 * A section that arrives in a later milestone. Hidden sections 404 so the URL does not
 * reveal more than the nav does; the API still enforces access on its own.
 */
export async function SectionPlaceholder({ section }: { section: Exclude<NavKey, "home"> }) {
  const viewer = await resolveViewer();
  const permissions = viewer.status === "ready" ? viewer.me.permissions : null;
  if (!isNavVisible(section, permissions)) notFound();
  const nav = await getTranslations("Nav");
  const t = await getTranslations("Placeholder");
  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 px-5 py-10 sm:px-8">
      <PageHeader title={nav(section)} />
      <EmptyState icon={<Hourglass aria-hidden />} title={t("title", { section: nav(section) })} body={t("body")} />
    </div>
  );
}

export async function sectionMetadata(locale: Locale, section: NavKey) {
  const t = await getTranslations({ locale, namespace: "Nav" });
  return { title: t(section) };
}
