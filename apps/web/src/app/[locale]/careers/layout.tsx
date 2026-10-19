// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";
import type * as React from "react";

import { Wordmark } from "@/components/shell/brand-mark";
import { LocaleSwitcher } from "@/components/shell/locale-switcher";
import { MAIN_CONTENT_ID } from "@/components/shell/skip-link";
import { ThemeToggle } from "@/components/shell/theme-toggle";

/**
 * Public careers pages: no sign-in (`/careers` is a public path), a quiet paper frame so
 * the organisation and its jobs come first. Mobile first; the index uses the full width
 * for its job grid, the job and apply pages keep a reading column.
 */
export default function CareersLayout({ children }: { children: React.ReactNode }) {
  const t = useTranslations("Careers");
  return (
    <div className="bg-ledger flex min-h-dvh flex-col">
      <header className="mx-auto flex w-full max-w-6xl items-center justify-between px-4 pt-5 sm:px-8">
        <span className="eyebrow">{t("eyebrow")}</span>
        <div className="flex items-center gap-0.5">
          <LocaleSwitcher />
          <ThemeToggle />
        </div>
      </header>
      <main id={MAIN_CONTENT_ID} tabIndex={-1} className="mx-auto w-full max-w-6xl flex-1 px-4 py-10 outline-none sm:px-8 sm:py-16">
        {children}
      </main>
      <footer className="mx-auto w-full max-w-6xl border-t border-border px-4 py-6 text-xs text-muted-foreground sm:px-8">
        <Wordmark className="origin-[0%_50%] scale-75 rtl:origin-[100%_50%]" />
      </footer>
    </div>
  );
}
