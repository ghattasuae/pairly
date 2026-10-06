// SPDX-License-Identifier: AGPL-3.0-only
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

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        disabled={orgs.length === 0 || pending}
        aria-label={t("switch")}
        className="flex h-9 max-w-56 min-w-0 items-center gap-2 rounded-md border border-transparent px-2.5 text-sm hover:bg-accent disabled:cursor-default disabled:hover:bg-transparent data-[state=open]:bg-accent"
      >
        <Building2 className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="truncate font-medium">{pending ? t("switching") : (active?.name ?? t("none"))}</span>
        {orgs.length > 0 && <ChevronsUpDown className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
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
