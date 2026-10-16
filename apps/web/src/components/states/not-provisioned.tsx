// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { UserRoundCog } from "lucide-react";
import { useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";
import { isolate } from "@/lib/bidi";

/**
 * Shown when the API answers 403 to /v1/me: the user is signed in and the token is valid,
 * but the API has no user or membership for them in this organisation yet.
 */
export function NotProvisioned({ email, code }: { email: string; code: string }) {
  const t = useTranslations("NotProvisioned");
  return (
    <section aria-labelledby="not-provisioned-heading" className="rise max-w-2xl space-y-6">
      <div className="flex size-12 items-center justify-center rounded-xl border bg-surface text-primary">
        <UserRoundCog className="size-6" aria-hidden />
      </div>
      <div className="space-y-3">
        <p className="eyebrow">{t("eyebrow")}</p>
        <h1 id="not-provisioned-heading" className="font-display text-3xl leading-tight sm:text-4xl">
          {t("title")}
        </h1>
        <p className="text-muted-foreground">{t("body", { email: isolate(email) })}</p>
      </div>
      <div className="rounded-xl border bg-surface p-5">
        <h2 className="mb-3 text-sm font-semibold">{t("next")}</h2>
        <ol className="space-y-2 text-sm text-muted-foreground">
          {(["step1", "step2"] as const).map((step, index) => (
            <li key={step} className="flex gap-3">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-secondary font-mono text-[0.7rem] text-secondary-foreground">
                {index + 1}
              </span>
              <span>{t(step)}</span>
            </li>
          ))}
        </ol>
      </div>
      <div className="space-y-2 text-sm text-muted-foreground">
        <p>
          {t("candidate")}{" "}
          <Link href="/portal" className="link-inline font-medium" data-portal-link>
            {t("candidateLink")}
          </Link>
        </p>
        <p>{t("referee")}</p>
      </div>
      <p className="font-mono text-xs text-muted-foreground">{t("reference", { code })}</p>
    </section>
  );
}
