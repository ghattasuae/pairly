// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Command } from "cmdk";
import { FileText, Languages, LogOut, Moon, ScrollText, Search } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import type * as React from "react";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { usePathname, useRouter } from "@/i18n/navigation";
import { searchEverything } from "@/lib/actions/search";
import type { SearchHit } from "@/lib/api/types";
import type { NavItem } from "@/lib/nav";
import { hitHref } from "@/lib/search-links";

import { NAV_ICONS } from "./nav-icons";

type SearchFn = (query: string) => Promise<{ ok: true; hits: SearchHit[] } | { ok: false; code: string }>;

interface PaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  items: readonly NavItem[];
  onSignOut: () => void;
  /** Override for stories and tests. Defaults to the server action. */
  search?: SearchFn;
}

/**
 * Command palette (Cmd/Ctrl+K): jump to sections, run shell actions, and search records
 * (`/v1/search`, two characters or more). The API only searches kinds the person may read.
 */
export function CommandPalette({ open, onOpenChange, ...rest }: PaletteProps) {
  const t = useTranslations("Command");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="overflow-hidden p-0">
        <DialogTitle className="sr-only">{t("title")}</DialogTitle>
        <DialogDescription className="sr-only">{t("description")}</DialogDescription>
        {/* Mounted only while open, so the query starts empty every time. */}
        <PaletteBody onOpenChange={onOpenChange} {...rest} />
      </DialogContent>
    </Dialog>
  );
}

function PaletteBody({ onOpenChange, items, onSignOut, search = searchEverything }: Omit<PaletteProps, "open">) {
  const t = useTranslations("Command");
  const nav = useTranslations("Nav");
  const router = useRouter();
  const pathname = usePathname();
  const locale = useLocale();
  const { resolvedTheme, setTheme } = useTheme();
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<{ query: string; hits: SearchHit[] } | null>(null);

  const q = query.trim();
  const active = q.length >= 2;
  const hits = active && result?.query === q ? result.hits : [];
  const searching = active && result?.query !== q;

  useEffect(() => {
    if (q.length < 2) return;
    let current = true;
    const timer = window.setTimeout(() => {
      search(q)
        .then((found) => current && setResult({ query: q, hits: found.ok ? found.hits : [] }))
        .catch(() => current && setResult({ query: q, hits: [] }));
    }, 250);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [q, search]);

  const run = (action: () => void) => () => {
    onOpenChange(false);
    action();
  };

  return (
    <Command
      label={t("title")}
      className="flex flex-col [&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pt-3 [&_[cmdk-group-heading]]:pb-1.5"
    >
      <div className="flex items-center gap-2 border-b px-3">
        <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <Command.Input
          value={query}
          onValueChange={setQuery}
          placeholder={t("placeholder")}
          className="h-12 w-full bg-transparent text-base outline-none placeholder:text-muted-foreground md:text-sm"
        />
        <Kbd>Esc</Kbd>
      </div>
      <Command.List className="max-h-80 overflow-y-auto p-1.5">
        <Command.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">
          {searching ? t("searching") : t("empty")}
        </Command.Empty>
        {hits.length > 0 && (
          <Command.Group heading={t("results")}>
            {hits.map((hit) => {
              const href = hitHref(hit);
              if (!href) return null;
              return (
                <PaletteItem
                  key={hit.chunk_id}
                  // The query is part of the value so cmdk keeps every hit the API found.
                  value={`hit ${hit.chunk_id} ${query}`}
                  onSelect={run(() => router.push(href))}
                  icon={hit.kind === "policy" ? <ScrollText aria-hidden /> : <FileText aria-hidden />}
                >
                  <span className="block truncate">{hit.title}</span>
                  <span className="block truncate text-xs text-muted-foreground">{hit.text}</span>
                </PaletteItem>
              );
            })}
          </Command.Group>
        )}
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
      <span className="min-w-0 flex-1">{children}</span>
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
