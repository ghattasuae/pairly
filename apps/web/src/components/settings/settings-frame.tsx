// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { ChevronLeft } from "lucide-react";
import { useTranslations } from "next-intl";
import type * as React from "react";

import { PageHeader } from "@/components/states/page-header";
import { Link } from "@/i18n/navigation";

/** The same frame for every settings page: back to Settings, a title, one line of help. */
export function SettingsFrame({
  title,
  subtitle,
  eyebrow,
  children,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  eyebrow?: React.ReactNode;
  children: React.ReactNode;
}) {
  const nav = useTranslations("Nav");
  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-5 py-10 sm:px-8">
      <Link
        href="/settings"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
      >
        <ChevronLeft className="size-4 rtl:rotate-180" aria-hidden />
        {nav("settings")}
      </Link>
      <PageHeader eyebrow={eyebrow} title={title}>
        {subtitle && <p>{subtitle}</p>}
      </PageHeader>
      {children}
    </div>
  );
}
