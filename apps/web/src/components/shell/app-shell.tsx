// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Menu, Search, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useState, useSyncExternalStore } from "react";
import type * as React from "react";

import { NotificationBell } from "@/components/notifications/notification-bell";
import { NotificationsProvider } from "@/components/notifications/notifications-provider";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { Toaster } from "@/components/ui/toaster";
import { useRouter } from "@/i18n/navigation";
import { authClient } from "@/lib/auth/client";
import type { NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

import { Breadcrumbs } from "./breadcrumbs";
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
  /** Live notifications (bell and stream). Off until the API knows the person. */
  notifications?: boolean;
}

const subscribeNoop = () => () => {};

export function AppShell({
  items,
  user,
  orgs,
  activeOrgId,
  children,
  onSignOut,
  onSwitchOrg,
  notifications = false,
}: AppShellProps) {
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

  const orgSwitcher = (
    <OrgSwitcher orgs={orgs} activeOrgId={activeOrgId} {...(onSwitchOrg ? { onSwitch: onSwitchOrg } : {})} />
  );
  const sidebar = <Sidebar items={items} onNavigate={() => setMenuOpen(false)} footer={orgSwitcher} />;
  const activeOrgName = orgs.find((org) => org.id === activeOrgId)?.name ?? null;

  return (
    <NotificationsProvider enabled={notifications}>
      <Toaster>
        <div className="flex min-h-dvh bg-background">
          {/* The desktop rail stays the first <aside> in the document (the RTL audit reads it). */}
          <aside className="sticky top-0 hidden h-dvh w-[4.25rem] shrink-0 border-e border-sidebar-border bg-sidebar lg:block xl:w-60">
            {sidebar}
          </aside>

          {menuOpen && (
            <div className="fixed inset-0 z-40 lg:hidden">
              <button
                type="button"
                aria-label={t("closeMenu")}
                className="absolute inset-0 bg-foreground/30"
                onClick={() => setMenuOpen(false)}
              />
              <aside className="absolute inset-y-0 start-0 w-64 border-e bg-sidebar shadow-raised animate-in slide-in-from-start">
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
            <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border bg-background/85 px-3 backdrop-blur-md supports-[backdrop-filter]:bg-background/75 sm:gap-3 sm:px-6">
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
              <Breadcrumbs items={items} orgName={activeOrgName} />
              <div className="flex-1" />
              <button
                type="button"
                onClick={() => setPaletteOpen(true)}
                aria-keyshortcuts={apple ? "Meta+K" : "Control+K"}
                className={cn(
                  "hidden h-9 w-72 items-center gap-2.5 rounded-md border border-border bg-surface ps-3 pe-1.5 text-sm text-muted-foreground hover:border-foreground/30 hover:text-foreground md:flex",
                )}
              >
                <Search className="size-4 shrink-0" aria-hidden />
                <span className="flex-1 truncate text-start">{t("search")}</span>
                <span className="flex gap-0.5" dir="ltr" aria-hidden>
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
              <div className="flex items-center gap-0.5">
                {notifications && <NotificationBell />}
                <LocaleSwitcher />
                <ThemeToggle />
              </div>
              <span aria-hidden className="mx-1 hidden h-5 w-px bg-border sm:block" />
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
      </Toaster>
    </NotificationsProvider>
  );
}
