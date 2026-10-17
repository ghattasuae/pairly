// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import type { DemoAccount } from "@/lib/demo/policy";

import { FormError } from "./auth-card";

export const DEMO_ERRORS = ["rate_limited", "unknown_account", "unavailable"] as const;
export type DemoError = (typeof DEMO_ERRORS)[number];

/**
 * The hosted demo's sign-in choices: one plain form post per seeded demo user
 * (`POST /api/demo/signin`, lib/demo/policy.ts). Works without JavaScript.
 */
export function DemoSigninList({ accounts, locale, error }: { accounts: DemoAccount[]; locale: string; error?: DemoError | null }) {
  const t = useTranslations("Demo");
  return (
    <div className="space-y-4">
      {error && <FormError id="demo-signin-error">{t(`errors.${error}`)}</FormError>}
      <ul className="space-y-2" aria-label={t("listLabel")}>
        {accounts.map((account) => (
          <li key={account.email}>
            <form method="post" action="/api/demo/signin" className="flex items-center justify-between gap-3 rounded-md border px-3 py-2">
              <input type="hidden" name="email" value={account.email} />
              <input type="hidden" name="locale" value={locale} />
              <span className="min-w-0">
                <span className="block text-sm font-medium">{t(`accounts.${account.label}` as "accounts.recruiter")}</span>
                <span className="block truncate text-xs text-muted-foreground" dir="ltr">
                  {account.email}
                </span>
              </span>
              <Button type="submit" size="sm">
                {t("signInAs")}
              </Button>
            </form>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">{t("note")}</p>
    </div>
  );
}
