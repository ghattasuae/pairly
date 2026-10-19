// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useTranslations } from "next-intl";
import type * as React from "react";

import { cn } from "@/lib/utils";

export interface SummaryTile {
  /** In-page anchor of the dashboard this tile sums up (its heading id). */
  anchor: string;
  title: string;
  /** Items waiting on this person in that dashboard. */
  count: number;
  /** How many "needs you" lists the count comes from. */
  lists: number;
}

/**
 * The stat strip at the top of Home: first the total waiting on you (the one saffron
 * number on the page), then one tile per dashboard. Numbers in Fraunces, the small print
 * in Plex Mono. Each tile jumps to its dashboard below.
 */
export function HomeSummary({ total, tiles, now }: { total: number; tiles: readonly SummaryTile[]; now: Date }) {
  const t = useTranslations("Home");
  const format = useFormatter();
  return (
    <section aria-label={t("summary")} className="rise" style={{ "--rise-index": 1 } as React.CSSProperties}>
      <ul className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-3 lg:grid-cols-4">
        <li className="col-span-2 flex flex-col justify-between gap-6 bg-surface p-5 sm:col-span-1">
          <p className="eyebrow">{t("waitingOnYou")}</p>
          <div className="space-y-1">
            <p className={cn("figure text-5xl leading-none", total > 0 ? "text-signal-ink" : "text-foreground")} data-summary-total={total}>
              {format.number(total)}
            </p>
            <p className="font-mono text-xs text-muted-foreground">
              {format.dateTime(now, { weekday: "short", day: "numeric", month: "short" })}
            </p>
          </div>
        </li>
        {tiles.map((tile) => (
          <li key={tile.anchor} className="bg-surface">
            <a
              href={`#${tile.anchor}`}
              className="group flex h-full flex-col justify-between gap-6 p-5 hover:bg-accent/60 focus-visible:-outline-offset-2"
            >
              <span className="text-sm font-medium text-foreground group-hover:underline group-hover:underline-offset-4">{tile.title}</span>
              <span className="space-y-1">
                <span className="figure block text-4xl leading-none">{format.number(tile.count)}</span>
                <span className="block font-mono text-xs text-muted-foreground">{t("tileLists", { count: tile.lists })}</span>
              </span>
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
