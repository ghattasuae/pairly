// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";

import { Link, usePathname } from "@/i18n/navigation";
import type { NavItem } from "@/lib/nav";

import { isActive } from "./sidebar";

/**
 * Where you are: the organisation, then the section of the rail you are in. The section
 * is a link back to its start page when you are deeper inside it, plain text (marked as
 * the current page) when you are on it. Only sections the rail shows can appear here.
 */
export function Breadcrumbs({ items, orgName }: { items: readonly NavItem[]; orgName: string | null }) {
  const t = useTranslations("Shell");
  const nav = useTranslations("Nav");
  const pathname = usePathname();
  // The most specific match wins ("/" only matches itself).
  const section = items.filter((item) => isActive(pathname, item.href)).at(-1);
  const atStart = section ? pathname === section.href : false;

  return (
    <nav aria-label={t("breadcrumb")} className="min-w-0">
      <ol className="flex min-w-0 items-center gap-1.5 text-sm">
        {orgName && (
          <li className="hidden min-w-0 items-center gap-1.5 text-muted-foreground sm:flex">
            <span className="max-w-48 truncate" dir="auto">
              {orgName}
            </span>
            {section && <ChevronRight className="size-3.5 shrink-0 opacity-60 rtl:-scale-x-100" aria-hidden />}
          </li>
        )}
        {section && (
          <li className="min-w-0 truncate font-medium text-foreground">
            {atStart ? (
              <span aria-current="page">{nav(section.key)}</span>
            ) : (
              <Link href={section.href} className="rounded-sm hover:underline hover:underline-offset-4">
                {nav(section.key)}
              </Link>
            )}
          </li>
        )}
      </ol>
    </nav>
  );
}
