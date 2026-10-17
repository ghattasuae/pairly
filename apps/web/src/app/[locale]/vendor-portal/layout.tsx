// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { getTranslations } from "next-intl/server";
import type * as React from "react";

import { Wordmark } from "@/components/shell/brand-mark";
import { LocaleSwitcher } from "@/components/shell/locale-switcher";
import { MAIN_CONTENT_ID } from "@/components/shell/skip-link";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { VendorPortalSignOut } from "@/components/vendor-portal/vendor-sign-out";
import { Link } from "@/i18n/navigation";
import { readVendorPortalToken } from "@/lib/vendor-portal/session";

/**
 * The vendor portal (`/vendor-portal` is a public path: a vendor's contacts have no staff
 * sign-in; the portal session is its own cookie). A quiet frame, mobile first, one
 * readable column.
 */
export default async function VendorPortalLayout({ children }: { children: React.ReactNode }) {
  const t = await getTranslations("VendorPortal");
  const signedIn = (await readVendorPortalToken()) !== null;
  return (
    <div className="bg-ledger flex min-h-dvh flex-col">
      <header className="mx-auto flex w-full max-w-3xl flex-wrap items-center justify-between gap-2 px-4 pt-4 sm:px-6">
        <span className="text-sm tracking-wide text-muted-foreground uppercase">{t("eyebrow")}</span>
        <div className="flex items-center gap-1">
          {signedIn && (
            <nav aria-label={t("nav.label")} className="flex items-center gap-1 text-sm">
              <Link href="/vendor-portal" className="rounded-md px-2 py-1 hover:bg-accent">
                {t("nav.invoices")}
              </Link>
              <Link href="/vendor-portal/details" className="rounded-md px-2 py-1 hover:bg-accent">
                {t("nav.details")}
              </Link>
              <VendorPortalSignOut />
            </nav>
          )}
          <LocaleSwitcher />
          <ThemeToggle />
        </div>
      </header>
      <main id={MAIN_CONTENT_ID} tabIndex={-1} className="mx-auto w-full max-w-3xl flex-1 px-4 py-8 outline-none sm:px-6 sm:py-12">
        {children}
      </main>
      <footer className="mx-auto w-full max-w-3xl px-4 pb-8 text-xs text-muted-foreground sm:px-6">
        <Wordmark className="scale-75 opacity-70" />
      </footer>
    </div>
  );
}
