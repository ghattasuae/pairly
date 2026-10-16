// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { CloudOff } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";

/** The API could not be reached or answered unexpectedly. Nothing is shown (fail closed). */
export function Unavailable({ retryHref }: { retryHref: string }) {
  const t = useTranslations("Unavailable");
  const common = useTranslations("Common");
  return (
    <section className="rise max-w-xl space-y-4" aria-labelledby="unavailable-heading">
      <CloudOff className="size-8 text-muted-foreground" aria-hidden />
      <h1 id="unavailable-heading" className="font-display text-3xl">
        {t("title")}
      </h1>
      <p className="text-muted-foreground">{t("body")}</p>
      <Button asChild variant="outline">
        <a href={retryHref}>{common("tryAgain")}</a>
      </Button>
    </section>
  );
}
