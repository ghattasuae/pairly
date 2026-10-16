// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { LifeBuoy } from "lucide-react";
import { useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";

/** Shown after a sign-in with a backup code: say so, and how many codes are left. */
export function BackupCodeNotice({ remaining }: { remaining: number | null }) {
  const t = useTranslations("BackupCode");
  return (
    <section
      role="status"
      aria-labelledby="backup-code-heading"
      className="flex items-start gap-3 rounded-xl border border-signal/40 bg-signal-soft/50 p-4 text-sm"
    >
      <LifeBuoy className="mt-0.5 size-5 shrink-0 text-signal-foreground dark:text-signal" aria-hidden />
      <div className="space-y-1">
        <h2 id="backup-code-heading" className="font-medium">
          {t("title")}
        </h2>
        <p className="text-muted-foreground">
          {remaining === null ? t("unknown") : t("remaining", { count: remaining })}{" "}
          <Link href="/settings/security" className="link-inline font-medium">
            {t("manage")}
          </Link>
        </p>
      </div>
    </section>
  );
}
