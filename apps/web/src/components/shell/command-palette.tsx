// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Command } from "cmdk";
import { Languages, LogOut, Moon, Search } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import type * as React from "react";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { usePathname, useRouter } from "@/i18n/navigation";
import type { NavItem } from "@/lib/nav";

import { NAV_ICONS } from "./nav-icons";

/**
 * Command palette (Cmd/Ctrl+K). A stub for now: it jumps to sections and runs a few shell
 * actions. Later work packages register record search and workflow actions here.
 */
export function CommandPalette({
  open,
  onOpenChange,
  items,
  onSignOut,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  items: readonly NavItem[];
  onSignOut: () => void;
}) {
  const t = useTranslations("Command");
  const nav = useTranslations("Nav");
  const router = useRouter();
  const pathname = usePathname();
  const locale = useLocale();
  const { resolvedTheme, setTheme } = useTheme();

  const run = (action: () => void) => () => {
    onOpenChange(false);
    action();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="overflow-hidden p-0">
        <DialogTitle className="sr-only">{t("title")}</DialogTitle>
        <DialogDescription className="sr-only">{t("description")}</DialogDescription>
        <Command
          label={t("title")}
          className="flex flex-col [&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pt-3 [&_[cmdk-group-heading]]:pb-1.5"
        >
          <div className="flex items-center gap-2 border-b px-3">
            <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            <Command.Input
              placeholder={t("placeholder")}
              className="h-12 w-full bg-transparent text-base outline-none placeholder:text-muted-foreground md:text-sm"
            />
            <Kbd>Esc</Kbd>
          </div>
          <Command.List className="max-h-80 overflow-y-auto p-1.5">
            <Command.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">
              {t("empty")}
            </Command.Empty>
            <Command.Group heading={t("navigate")}>
              {items.map((item) => {
                const Icon = NAV_ICONS[item.key];
                return (
                  <PaletteItem
                    key={item.key}
                    value={`nav ${item.key} ${nav(item.key)}`}
                    onSelect={run(() => router.push(item.href))}
                    icon={<Icon aria-hidden />}
                    keys={["G", item.shortcut]}
                  >
                    {nav(item.key)}
                  </PaletteItem>
                );
              })}
            </Command.Group>
            <Command.Group heading={t("actions")}>
              <PaletteItem
                value={`theme ${t("toggleTheme")}`}
                onSelect={run(() => setTheme(resolvedTheme === "dark" ? "light" : "dark"))}
                icon={<Moon aria-hidden />}
              >
                {t("toggleTheme")}
              </PaletteItem>
              <PaletteItem
                value={`language ${t("switchLanguage")}`}
                onSelect={run(() => router.replace(pathname, { locale: locale === "ar" ? "en" : "ar" }))}
                icon={<Languages aria-hidden />}
              >
                {t("switchLanguage")}
              </PaletteItem>
              <PaletteItem value={`sign out ${t("signOut")}`} onSelect={run(onSignOut)} icon={<LogOut aria-hidden />}>
                {t("signOut")}
              </PaletteItem>
            </Command.Group>
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  );
}

function PaletteItem({
  children,
  icon,
  keys,
  ...props
}: React.ComponentProps<typeof Command.Item> & { icon: React.ReactNode; keys?: string[] }) {
  return (
    <Command.Item
      className="flex cursor-default items-center gap-3 rounded-md px-3 py-2 text-sm select-none data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-muted-foreground"
      {...props}
    >
      {icon}
      <span className="flex-1">{children}</span>
      {keys && (
        <span className="flex gap-1" aria-hidden>
          {keys.map((key) => (
            <Kbd key={key}>{key}</Kbd>
          ))}
        </span>
      )}
    </Command.Item>
  );
}
