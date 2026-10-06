// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Menu, Search, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useState, useSyncExternalStore } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { useRouter } from "@/i18n/navigation";
import { authClient } from "@/lib/auth/client";
import type { NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

import { CommandPalette } from "./command-palette";
import { LocaleSwitcher } from "./locale-switcher";
import { OrgSwitcher, type OrgOption } from "./org-switcher";
import { isApplePlatform, useGlobalShortcuts } from "./shortcuts";
import { Sidebar } from "./sidebar";
import { MAIN_CONTENT_ID } from "./skip-link";
import { ThemeToggle } from "./theme-toggle";
import { type ShellUser, UserMenu } from "./user-menu";

export interface AppShellProps {
  items: readonly NavItem[];
  user: ShellUser;
  orgs: readonly OrgOption[];
  activeOrgId: string | null;
  children: React.ReactNode;
  /** Override for stories and tests. Defaults to Better Auth sign-out. */
  onSignOut?: () => Promise<void> | void;
  /** Override for stories and tests. Defaults to Better Auth. */
  onSwitchOrg?: (orgId: string) => Promise<void>;
}

const subscribeNoop = () => () => {};

export function AppShell({ items, user, orgs, activeOrgId, children, onSignOut, onSwitchOrg }: AppShellProps) {
  const t = useTranslations("Shell");
  const router = useRouter();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const apple = useSyncExternalStore(subscribeNoop, isApplePlatform, () => true);

  const signOut = useCallback(async () => {
    if (onSignOut) {
      await onSignOut();
      return;
    }
    await authClient.signOut();
    router.replace("/sign-in");
    router.refresh();
  }, [onSignOut, router]);

  const togglePalette = useCallback(() => setPaletteOpen((open) => !open), []);
  const navigate = useCallback((href: string) => router.push(href), [router]);
  useGlobalShortcuts({ onTogglePalette: togglePalette, items, onNavigate: navigate });

  const sidebar = <Sidebar items={items} onNavigate={() => setMenuOpen(false)} />;

  return (
    <div className="flex min-h-dvh bg-background">
      <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 border-e border-sidebar-border bg-sidebar lg:block">
        {sidebar}
      </aside>

      {menuOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            aria-label={t("closeMenu")}
            className="absolute inset-0 bg-foreground/25"
            onClick={() => setMenuOpen(false)}
          />
          <aside className="absolute inset-y-0 start-0 w-64 border-e bg-sidebar shadow-soft animate-in slide-in-from-start">
            <Button
              variant="ghost"
              size="icon"
              className="absolute end-2 top-4"
              aria-label={t("closeMenu")}
              onClick={() => setMenuOpen(false)}
            >
              <X aria-hidden />
            </Button>
            {sidebar}
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b bg-background/85 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/70 sm:px-5">
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden"
            aria-label={t("openMenu")}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(true)}
          >
            <Menu aria-hidden />
          </Button>
          <OrgSwitcher orgs={orgs} activeOrgId={activeOrgId} {...(onSwitchOrg ? { onSwitch: onSwitchOrg } : {})} />
          <div className="flex-1" />
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            aria-keyshortcuts={apple ? "Meta+K" : "Control+K"}
            className={cn(
              "hidden h-9 w-64 items-center gap-2 rounded-md border bg-surface px-3 text-sm text-muted-foreground transition-colors hover:border-ring/50 md:flex",
            )}
          >
            <Search className="size-4" aria-hidden />
            <span className="flex-1 text-start">{t("search")}</span>
            <span className="flex gap-1" aria-hidden>
              <Kbd>{apple ? "⌘" : "Ctrl"}</Kbd>
              <Kbd>K</Kbd>
            </span>
          </button>
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            aria-label={t("search")}
            onClick={() => setPaletteOpen(true)}
          >
            <Search aria-hidden />
          </Button>
          <LocaleSwitcher />
          <ThemeToggle />
          <UserMenu user={user} onSignOut={() => void signOut()} />
        </header>

        <main id={MAIN_CONTENT_ID} tabIndex={-1} className="flex-1 outline-none">
          {children}
        </main>
      </div>

      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        items={items}
        onSignOut={() => void signOut()}
      />
    </div>
  );
}
