// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { useLocale, useTranslations } from "next-intl";
import type * as React from "react";

import { Kbd } from "@/components/ui/kbd";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Link, usePathname } from "@/i18n/navigation";
import { localeDirection } from "@/i18n/routing";
import type { NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

import { BrandMark } from "./brand-mark";
import { NAV_ICONS } from "./nav-icons";

export function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * Role-aware navigation, drawn as a slim rail. Between lg and xl it shows icons only (the
 * label stays in the accessibility tree and appears on hover as a tooltip with the G
 * shortcut); from xl, and in the mobile drawer, the labels show. The current section is
 * marked by a saffron bar on the rail's inner edge. The items arrive already filtered
 * by permissions. `footer` sits at the bottom (the organisation switcher).
 */
export function Sidebar({
  items,
  onNavigate,
  footer,
}: {
  items: readonly NavItem[];
  onNavigate?: () => void;
  footer?: React.ReactNode;
}) {
  const t = useTranslations("Nav");
  const common = useTranslations("Common");
  const pathname = usePathname();
  // Radix sides are physical: open the hint away from the sidebar in both directions.
  const hintSide = localeDirection[useLocale()] === "rtl" ? "left" : "right";

  return (
    <div className="flex h-full flex-col gap-7 px-3 py-5">
      <div className="flex h-8 items-center px-1.5 lg:max-xl:justify-center lg:max-xl:px-0">
        <Link href="/" onClick={onNavigate} dir="ltr" className="inline-flex items-center gap-2 rounded-md text-foreground">
          <BrandMark className="size-7" />
          <span className="font-display text-xl font-medium tracking-tight lg:max-xl:sr-only">{common("appName")}</span>
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
                        "group relative flex h-9 items-center gap-3 rounded-md px-2.5 text-sm text-muted-foreground hover:bg-sidebar-active/70 hover:text-foreground lg:max-xl:justify-center lg:max-xl:px-0",
                        active && "bg-sidebar-active font-medium text-foreground",
                      )}
                    >
                      <span
                        aria-hidden
                        className={cn(
                          "absolute inset-y-2 -start-3 w-[3px] rounded-e-full bg-signal opacity-0 transition-opacity duration-150 rtl:rounded-e-none rtl:rounded-s-full",
                          active && "opacity-100",
                        )}
                      />
                      <Icon className={cn("size-[1.05rem] shrink-0", active && "text-foreground")} aria-hidden />
                      <span className="flex-1 truncate lg:max-xl:sr-only">{t(item.key)}</span>
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
      {footer && <div className="border-t border-sidebar-border pt-3">{footer}</div>}
    </div>
  );
}
