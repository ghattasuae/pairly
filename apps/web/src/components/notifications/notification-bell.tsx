// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Bell, BellRing, CheckCheck, Settings2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Link } from "@/i18n/navigation";
import { inAppPath } from "@/lib/in-app-link";
import { cn } from "@/lib/utils";

import { useNotifications } from "./notifications-provider";

/** The bell in the top bar: unread count (live), the newest items, mark read. */
export function NotificationBell() {
  const t = useTranslations("Notifications");
  const format = useFormatter();
  const router = useRouter();
  const { items, unread, markRead, markAllRead, loaded } = useNotifications();
  const shown = items.slice(0, 8);
  const label = unread > 0 ? t("bellUnread", { count: unread }) : t("bell");

  const open = (id: string, link: string | null) => {
    void markRead(id);
    const path = inAppPath(link);
    // Paths from the API carry their own locale prefix: the plain Next router, not next-intl's.
    if (path) router.push(path);
  };

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger
        aria-label={label}
        className="relative flex size-9 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground [&_svg]:size-4"
      >
        {unread > 0 ? <BellRing aria-hidden /> : <Bell aria-hidden />}
        {unread > 0 && (
          <span
            aria-hidden
            dir="ltr"
            className="absolute -top-0.5 -end-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-signal px-1 font-mono text-[0.6rem] font-semibold text-signal-foreground"
          >
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80 p-0">
        <DropdownMenuLabel className="px-3 py-2.5 normal-case tracking-normal">
          <span className="text-sm font-medium text-foreground">{t("title")}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator className="my-0" />
        {shown.length === 0 ? (
          <DropdownMenuLabel className="px-3 py-6 text-center text-sm font-normal normal-case tracking-normal text-muted-foreground">
            {loaded ? t("empty") : t("loading")}
          </DropdownMenuLabel>
        ) : (
          <DropdownMenuGroup className="max-h-96 overflow-y-auto py-1">
            {shown.map((item) => (
              <DropdownMenuItem
                key={item.id}
                onSelect={() => open(item.id, item.link)}
                className="flex items-start gap-2.5 px-3 py-2.5"
              >
                <span
                  aria-hidden
                  className={cn("mt-1.5 size-2 shrink-0 rounded-full", item.read_at ? "bg-transparent" : "bg-signal")}
                />
                <span className="min-w-0 flex-1 space-y-0.5">
                  <span className="block text-sm font-medium text-foreground">
                    {item.title}
                    {!item.read_at && <span className="sr-only"> ({t("unread")})</span>}
                  </span>
                  {item.body && <span className="line-clamp-2 block text-xs text-muted-foreground">{item.body}</span>}
                  <span className="block text-xs text-muted-foreground">
                    {format.relativeTime(new Date(item.created_at), new Date())}
                  </span>
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        )}
        {unread > 0 && (
          <>
            <DropdownMenuSeparator className="my-0" />
            <DropdownMenuItem className="px-3 py-2.5" onSelect={() => void markAllRead()}>
              <CheckCheck aria-hidden />
              {t("markAllRead")}
            </DropdownMenuItem>
          </>
        )}
        <DropdownMenuSeparator className="my-0" />
        <DropdownMenuItem asChild className="px-3 py-2.5">
          <Link href="/settings/notifications">
            <Settings2 aria-hidden />
            {t("settings")}
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
