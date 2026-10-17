// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";
import type * as React from "react";

import { LocaleSwitcher } from "@/components/shell/locale-switcher";
import { Wordmark } from "@/components/shell/brand-mark";
import { MAIN_CONTENT_ID } from "@/components/shell/skip-link";
import { ThemeToggle } from "@/components/shell/theme-toggle";

/** Sign-in frame: brand panel on wide screens, the step on ruled paper. */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  const t = useTranslations("Common");
  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
      <aside className="relative hidden overflow-hidden bg-primary text-primary-foreground lg:flex lg:flex-col lg:justify-between lg:p-12">
        <div aria-hidden className="pointer-events-none absolute -end-24 -bottom-24 size-[28rem] rounded-full border-[3rem] border-primary-foreground/8" />
        <div aria-hidden className="pointer-events-none absolute -end-4 -bottom-40 size-[28rem] rounded-full border-[3rem] border-signal/25" />
        <span className="[&_*]:!text-primary-foreground">
          <Wordmark />
        </span>
        <p className="relative max-w-sm font-display text-4xl leading-tight italic">{t("tagline")}</p>
      </aside>
      <div className="bg-ledger flex flex-col">
        <header className="flex items-center justify-between px-6 pt-5 sm:px-10">
          <span className="lg:invisible">
            <Wordmark />
          </span>
          <div className="flex items-center gap-1">
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
