// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useLocale, useTranslations } from "next-intl";

import { Kbd } from "@/components/ui/kbd";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Link, usePathname } from "@/i18n/navigation";
import { localeDirection } from "@/i18n/routing";
import type { NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

import { Wordmark } from "./brand-mark";
import { NAV_ICONS } from "./nav-icons";

export function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Role-aware navigation. The items arrive already filtered by permissions. */
export function Sidebar({ items, onNavigate }: { items: readonly NavItem[]; onNavigate?: () => void }) {
  const t = useTranslations("Nav");
  const pathname = usePathname();
  // Radix sides are physical: open the hint away from the sidebar in both directions.
  const hintSide = localeDirection[useLocale()] === "rtl" ? "left" : "right";

  return (
    <div className="flex h-full flex-col gap-6 px-3 py-5">
      <div className="px-2">
        <Link href="/" onClick={onNavigate} className="inline-flex rounded-md">
          <Wordmark />
        </Link>
      </div>
      <nav aria-label={t("label")} className="flex-1">
        <ul className="flex flex-col gap-0.5">
          {items.map((item) => {
            const Icon = NAV_ICONS[item.key];
            const active = isActive(pathname, item.href);
            return (
              <li key={item.key}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Link
                      href={item.href}
                      onClick={onNavigate}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "group relative flex items-center gap-3 rounded-md px-2.5 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-active/60 hover:text-foreground",
                        active && "bg-sidebar-active font-medium text-foreground",
                      )}
                    >
                      <span
                        aria-hidden
                        className={cn(
                          "absolute inset-y-1.5 start-0 w-0.5 rounded-full bg-primary opacity-0 transition-opacity",
                          active && "opacity-100",
                        )}
                      />
                      <Icon className="size-4 shrink-0" aria-hidden />
                      <span className="flex-1">{t(item.key)}</span>
                    </Link>
                  </TooltipTrigger>
                  <TooltipContent side={hintSide}>
                    {t("goTo", { section: t(item.key) })}
                    <span className="flex gap-1">
                      <Kbd className="border-transparent">G</Kbd>
                      <Kbd className="border-transparent">{item.shortcut}</Kbd>
                    </span>
                  </TooltipContent>
                </Tooltip>
              </li>
            );
          })}
        </ul>
      </nav>
    </div>
  );
}
