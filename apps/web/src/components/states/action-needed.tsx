// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { CheckCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import type * as React from "react";

import { EmptyState } from "./empty-state";

/**
 * The "what needs me right now" section that opens every home dashboard (brief 5.1).
 * Items arrive in M1 with the approval inbox; until then it shows the calm empty state.
 */
export function ActionNeeded({ count = 0, children }: { count?: number; children?: React.ReactNode }) {
  const t = useTranslations("Home");
  return (
    <section
      aria-labelledby="action-needed-heading"
      className="relative rounded-lg border bg-surface ps-6 pe-5 before:absolute before:inset-y-4 before:start-0 before:w-[3px] before:rounded-e-full before:bg-signal rtl:before:rounded-e-none rtl:before:rounded-s-full"
    >
      <header className="flex items-center gap-2 pt-5">
        <h2 id="action-needed-heading" className="eyebrow">
          {t("actionNeeded")}
        </h2>
        <span className="rounded-sm bg-signal-soft px-1.5 py-0.5 font-mono text-[0.7rem] text-signal-foreground dark:text-signal">
          {count}
        </span>
      </header>
      {count > 0 && children ? (
        <div className="py-4">{children}</div>
      ) : (
        <EmptyState icon={<CheckCheck aria-hidden />} title={t("emptyTitle")} body={t("emptyBody")} className="py-6" />
      )}
    </section>
  );
}
