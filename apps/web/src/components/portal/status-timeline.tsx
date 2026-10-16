// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Circle, CircleDot } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { type PortalApplication, stepViews } from "@/lib/portal/steps";
import { cn } from "@/lib/utils";

/**
 * Where an application stands, in honest plain steps (brief 3.9.1): the steps reached with
 * their dates, the current one explained in one sentence, and the usual steps ahead.
 * Not colour alone: each state has an icon and words.
 */
export function StatusTimeline({ app, compact = false }: { app: PortalApplication; compact?: boolean }) {
  const t = useTranslations("Portal");
  const format = useFormatter();
  const steps = stepViews(app);
  return (
    <ol className="space-y-3" data-timeline={app.id}>
      {steps.map((s) => {
        const Icon = s.state === "done" ? Check : s.state === "current" ? CircleDot : Circle;
        return (
          <li
            key={`${s.step}-${s.at ?? "ahead"}`}
            className={cn("flex gap-3", s.state === "ahead" && "text-muted-foreground")}
            data-step={s.step}
            data-state={s.state}
            aria-current={s.state === "current" ? "step" : undefined}
          >
            <Icon
              className={cn(
                "mt-0.5 size-5 shrink-0",
                s.state === "current" ? "text-primary" : s.state === "done" ? "text-foreground" : "text-muted-foreground",
              )}
              aria-hidden
            />
            <div className="min-w-0">
              <p className={cn("text-sm", s.state === "current" && "font-semibold")}>
                {t(`steps.${s.step}.label`)}
                <span className="sr-only">: {t(`state.${s.state}`)}</span>
              </p>
              {s.at && (
                <p className="text-xs text-muted-foreground">
                  {format.dateTime(new Date(s.at), { dateStyle: "medium" })}
                </p>
              )}
              {s.state === "current" && !compact && (
                <p className="mt-1 text-sm text-muted-foreground">{t(`steps.${s.step}.help`)}</p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
