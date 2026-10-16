// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { Wordmark } from "@/components/shell/brand-mark";
import { LocaleSwitcher } from "@/components/shell/locale-switcher";
import { MAIN_CONTENT_ID } from "@/components/shell/skip-link";
import { ThemeToggle } from "@/components/shell/theme-toggle";

/**
 * The candidate experience survey (`/survey` is a public path: a signed single use
 * link, no sign-in). Phone first, one column.
 */
export default function SurveyLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-ledger flex min-h-dvh flex-col">
      <header className="mx-auto flex w-full max-w-3xl items-center justify-between gap-2 px-4 pt-4 sm:px-6">
        <Wordmark className="scale-75 opacity-80" />
        <div className="flex items-center gap-1">
          <LocaleSwitcher />
          <ThemeToggle />
        </div>
      </header>
      <main id={MAIN_CONTENT_ID} tabIndex={-1} className="mx-auto w-full max-w-3xl flex-1 px-4 py-6 outline-none sm:px-6 sm:py-10">
        {children}
      </main>
    </div>
  );
}
