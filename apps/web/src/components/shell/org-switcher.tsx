// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Building2, ChevronsUpDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useTransition } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { switchOrganization } from "@/lib/actions/orgs";

export interface OrgOption {
  id: string;
  name: string;
}

/**
 * Lists the organisations the API says the user belongs to (`GET /v1/me/orgs`) and switches
 * between them through a server action, which re-checks that list. The next server render
 * sends the id to the API as X-Pairly-Org, and the API decides what this user may do there.
 */
export function OrgSwitcher({
  orgs,
  activeOrgId,
  onSwitch,
}: {
  orgs: readonly OrgOption[];
  activeOrgId: string | null;
  /** Override for stories and tests. Defaults to the `switchOrganization` server action. */
  onSwitch?: (orgId: string) => Promise<void>;
}) {
  const t = useTranslations("Org");
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const active = orgs.find((org) => org.id === activeOrgId);

  const switchTo = (orgId: string) =>
    startTransition(async () => {
      if (onSwitch) await onSwitch(orgId);
      else await switchOrganization(orgId);
      router.refresh();
    });

  const name = pending ? t("switching") : (active?.name ?? t("none"));
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        disabled={orgs.length === 0 || pending}
        aria-label={t("switch")}
        className="group flex h-11 w-full min-w-0 items-center gap-2.5 rounded-md px-1.5 text-start text-sm hover:bg-sidebar-active/70 disabled:cursor-default disabled:hover:bg-transparent data-[state=open]:bg-sidebar-active lg:max-xl:justify-center lg:max-xl:px-0"
      >
        <span
          aria-hidden
          className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-surface font-display text-sm font-medium text-foreground"
        >
          {active ? <span dir="auto">{active.name.trim().charAt(0).toUpperCase()}</span> : <Building2 className="size-3.5 text-muted-foreground" />}
        </span>
        <span className="min-w-0 flex-1 lg:max-xl:sr-only">
          <span className="block text-[0.68rem] leading-tight text-muted-foreground">{t("label")}</span>
          <span className="block truncate leading-tight font-medium" dir="auto">
            {name}
          </span>
        </span>
        {orgs.length > 0 && (
          <ChevronsUpDown className="size-3.5 shrink-0 text-muted-foreground lg:max-xl:hidden" aria-hidden />
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" className="min-w-56">
        <DropdownMenuLabel>{t("label")}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={activeOrgId ?? ""} onValueChange={switchTo}>
          {orgs.map((org) => (
            <DropdownMenuRadioItem key={org.id} value={org.id}>
              <span className="truncate">{org.name}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
