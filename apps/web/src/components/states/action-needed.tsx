// SPDX-License-Identifier: AGPL-3.0-only
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
      className="relative rounded-xl border bg-surface ps-6 pe-5 shadow-soft before:absolute before:inset-y-4 before:start-0 before:w-1 before:rounded-e-full before:bg-signal"
    >
      <header className="flex items-center gap-2 pt-5">
        <h2 id="action-needed-heading" className="eyebrow">
          {t("actionNeeded")}
        </h2>
        <span className="rounded-full bg-signal-soft px-2 py-0.5 font-mono text-[0.7rem] text-signal-foreground dark:text-signal">
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
