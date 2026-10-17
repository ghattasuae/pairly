// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";

import { Wordmark } from "@/components/shell/brand-mark";
import { MAIN_CONTENT_ID } from "@/components/shell/skip-link";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";

export default function NotFound() {
  const t = useTranslations("NotFound");
  const common = useTranslations("Common");
  return (
    <div className="bg-ledger flex min-h-dvh flex-col px-6 py-8 sm:px-10">
      <header>
        <Wordmark />
      </header>
      <main id={MAIN_CONTENT_ID} tabIndex={-1} className="flex flex-1 items-center outline-none">
        <div className="rise max-w-lg space-y-4">
          <p className="font-mono text-sm text-signal-foreground dark:text-signal">{t("code")}</p>
          <h1 className="font-display text-5xl leading-none tracking-tight">{t("title")}</h1>
          <p className="text-lg text-muted-foreground">{t("body")}</p>
          <Button asChild variant="outline">
            <Link href="/">{common("backHome")}</Link>
          </Button>
        </div>
      </main>
    </div>
  );
}
