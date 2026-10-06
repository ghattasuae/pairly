// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { ChevronRight, ShieldCheck, Users } from "lucide-react";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { PageHeader } from "@/components/states/page-header";
import { Link } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { isNavVisible } from "@/lib/nav";
import { resolveViewer } from "@/lib/provisioning";

import { sectionMetadata } from "../section-placeholder";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return sectionMetadata(toLocale((await params).locale), "settings");
}

export default async function SettingsPage({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  const viewer = await resolveViewer();
  const permissions = viewer.status === "ready" ? viewer.me.permissions : null;
  if (!isNavVisible("settings", permissions)) notFound();
  const nav = await getTranslations("Nav");
  const t = await getTranslations("Settings");

  const links = [
    ...(permissions?.includes("core.users.read")
      ? [{ href: "/settings/members", title: nav("members"), body: t("membersBody"), icon: <Users aria-hidden /> }]
      : []),
    { href: "/settings/security", title: nav("security"), body: t("securityBody"), icon: <ShieldCheck aria-hidden /> },
  ];

  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-5 py-10 sm:px-8">
      <PageHeader title={nav("settings")}>
        <p>{t("subtitle")}</p>
      </PageHeader>
      <ul className="grid gap-3 sm:grid-cols-2">
        {links.map((link) => (
          <li key={link.href}>
            <Link
              href={link.href}
              className="group flex items-start gap-4 rounded-xl border bg-surface p-5 shadow-soft transition-colors hover:bg-accent"
            >
              <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border bg-background text-primary [&_svg]:size-5">
                {link.icon}
              </span>
              <span className="min-w-0 flex-1 space-y-1">
                <span className="block font-display text-xl">{link.title}</span>
                <span className="block text-sm text-muted-foreground">{link.body}</span>
              </span>
              <ChevronRight className="mt-1 size-4 text-muted-foreground rtl:rotate-180" aria-hidden />
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
