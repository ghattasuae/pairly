// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { LogOut, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Link } from "@/i18n/navigation";

export interface ShellUser {
  name: string | null;
  email: string;
}

export function initials(user: ShellUser): string {
  const source = user.name?.trim() || user.email;
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  return parts
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join("");
}

export function UserMenu({ user, onSignOut }: { user: ShellUser; onSignOut: () => void }) {
  const t = useTranslations("Shell");
  const nav = useTranslations("Nav");
  const common = useTranslations("Common");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={t("account")}
        className="flex size-9 items-center justify-center rounded-full bg-secondary text-xs font-semibold text-secondary-foreground ring-offset-2 ring-offset-background hover:ring-2 hover:ring-ring/40"
      >
        <span dir="ltr">{initials(user)}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="normal-case tracking-normal">
          <span className="block truncate text-sm font-medium text-foreground">{user.name || user.email}</span>
          <span className="block truncate text-xs font-normal text-muted-foreground">
            <bdi>{user.email}</bdi>
          </span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link href="/settings/security">
            <ShieldCheck aria-hidden />
            {nav("security")}
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onSignOut}>
          <LogOut aria-hidden />
          {common("signOut")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
