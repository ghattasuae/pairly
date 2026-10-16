// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";
import type * as React from "react";

import { Wordmark } from "@/components/shell/brand-mark";
import { LocaleSwitcher } from "@/components/shell/locale-switcher";
import { MAIN_CONTENT_ID } from "@/components/shell/skip-link";
import { ThemeToggle } from "@/components/shell/theme-toggle";

/**
 * Public careers pages: no sign-in (`/careers` is a public path), a quiet frame so the
 * organisation and its jobs come first. Mobile first, one column, readable line length.
 */
export default function CareersLayout({ children }: { children: React.ReactNode }) {
  const t = useTranslations("Careers");
  return (
    <div className="bg-ledger flex min-h-dvh flex-col">
      <header className="mx-auto flex w-full max-w-3xl items-center justify-between px-4 pt-4 sm:px-6">
        <span className="text-sm tracking-wide text-muted-foreground uppercase">{t("eyebrow")}</span>
        <div className="flex items-center gap-1">
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
