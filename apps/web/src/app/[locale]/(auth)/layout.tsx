// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";
import type * as React from "react";

import { LocaleSwitcher } from "@/components/shell/locale-switcher";
import { Wordmark } from "@/components/shell/brand-mark";
import { MAIN_CONTENT_ID } from "@/components/shell/skip-link";
import { ThemeToggle } from "@/components/shell/theme-toggle";

/**
 * Sign-in frame, split in two on wide screens: a statement panel on deeper paper (what
 * Pairly stands for, set in Fraunces) and the step itself. On phones only the step shows,
 * with the wordmark above it.
 */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  const t = useTranslations("Auth");
  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,6fr)_minmax(0,5fr)]">
      <aside className="relative hidden overflow-hidden border-e border-border bg-surface-sunken lg:flex lg:flex-col lg:justify-between lg:p-14 xl:p-20">
        <Wordmark />
        <div className="relative max-w-xl space-y-6">
          <span aria-hidden className="block h-[3px] w-12 rounded-full bg-signal" />
          <p className="eyebrow">{t("statementEyebrow")}</p>
          <p className="font-display text-5xl leading-[1.05] font-normal text-balance xl:text-6xl">{t("statement")}</p>
          <p className="max-w-md text-base leading-relaxed text-muted-foreground">{t("statementBody")}</p>
        </div>
        <LedgerRings />
      </aside>
      <div className="bg-ledger flex flex-col">
        <header className="flex items-center justify-between px-6 pt-5 sm:px-10">
          <span className="lg:invisible">
            <Wordmark />
          </span>
          <div className="flex items-center gap-0.5">
            <LocaleSwitcher />
            <ThemeToggle />
          </div>
        </header>
        <main id={MAIN_CONTENT_ID} tabIndex={-1} className="flex flex-1 items-center justify-center px-6 py-12 outline-none sm:px-10">
          {children}
        </main>
      </div>
    </div>
  );
}

/** The brand's two rings, drawn large in hairline ink, with the shared lens in saffron. */
function LedgerRings() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 400 220"
      className="pointer-events-none absolute -end-32 -bottom-32 w-[30rem] text-foreground/15 rtl:-scale-x-100"
    >
      <circle cx="150" cy="110" r="96" fill="none" stroke="currentColor" strokeWidth="1" />
      <circle cx="250" cy="110" r="96" fill="none" stroke="currentColor" strokeWidth="1" />
      <path d="M200 28.1a96 96 0 0 1 0 163.8a96 96 0 0 1 0-163.8z" fill="var(--signal)" opacity="0.18" />
    </svg>
  );
}
