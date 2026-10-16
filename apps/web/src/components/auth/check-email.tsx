// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { MailCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { useSyncExternalStore } from "react";

import { Link } from "@/i18n/navigation";
import { isolate } from "@/lib/bidi";

const subscribe = () => () => {};
function readEmail(): string | null {
  try {
    return sessionStorage.getItem("pairly.signInEmail");
  } catch {
    return null;
  }
}

export function CheckEmail({ showDevHint }: { showDevHint: boolean }) {
  const t = useTranslations("CheckEmail");
  const email = useSyncExternalStore(subscribe, readEmail, () => null);
  return (
    <div className="rise w-full max-w-sm space-y-6">
      <div className="flex size-12 items-center justify-center rounded-xl border bg-surface text-primary">
        <MailCheck className="size-6" aria-hidden />
      </div>
      <div className="space-y-2" role="status">
        <h1 className="font-display text-3xl leading-tight">{t("title")}</h1>
        <p className="text-muted-foreground">{email ? t("body", { email: isolate(email) }) : t("bodyNoEmail")}</p>
        <p className="text-sm text-muted-foreground">{t("expires")}</p>
      </div>
      {showDevHint && (
        <p className="rounded-md border border-dashed bg-surface-sunken px-3 py-2 font-mono text-xs text-muted-foreground">
          {t("devHint")}
        </p>
      )}
      <Link href="/sign-in" className="inline-block text-sm font-medium text-primary underline-offset-4 hover:underline">
        {t("useDifferent")}
      </Link>
    </div>
  );
}
