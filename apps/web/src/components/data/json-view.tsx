// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { EyeOff } from "lucide-react";
import { useTranslations } from "next-intl";
import type * as React from "react";

import { cn } from "@/lib/utils";

/** `{"redacted": true}`: a value the API hid for this person's role. */
export function isRedacted(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.keys(value).length === 1 &&
    (value as Record<string, unknown>).redacted === true
  );
}

/** `invoice_total` reads as "Invoice total". Keys come from workflows, so they stay plain. */
export function humanizeKey(key: string): string {
  const words = key.replace(/[_.-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

export function Redacted() {
  const t = useTranslations("Data");
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-xs text-muted-foreground">
      <EyeOff className="size-3" aria-hidden />
      {t("redacted")}
    </span>
  );
}

function Scalar({ value }: { value: unknown }) {
  const t = useTranslations("Data");
  if (value === null || value === undefined || value === "") return <span className="text-muted-foreground">{t("none")}</span>;
  if (typeof value === "boolean") return <span>{value ? t("yes") : t("no")}</span>;
  if (typeof value === "number") return <bdi className="font-mono tabular-nums">{value}</bdi>;
  // Text from documents and workflows: always rendered as text, never as HTML.
  return <bdi className="break-words whitespace-pre-wrap">{String(value)}</bdi>;
}

/**
 * Any JSON value as a readable list: objects become labelled rows, arrays become lists,
 * hidden values show "Hidden for your role". Depth is capped so a huge payload cannot
 * flood the page.
 */
export function JsonView({
  value,
  className,
  depth = 0,
  renderValue,
}: {
  value: unknown;
  className?: string;
  depth?: number;
  /** Custom rendering for a key (for example ids that link somewhere). Return undefined to fall back. */
  renderValue?: (key: string, value: unknown) => React.ReactNode | undefined;
}) {
  const t = useTranslations("Data");
  if (isRedacted(value)) return <Redacted />;
  if (depth > 6) return <span className="text-muted-foreground">…</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-muted-foreground">{t("none")}</span>;
    return (
      <ol className={cn("space-y-2", className)}>
        {value.slice(0, 200).map((item, index) => (
          <li key={index} className={cn(typeof item === "object" && item !== null && "rounded-md border bg-surface-sunken/60 p-3")}>
            <JsonView value={item} depth={depth + 1} {...(renderValue ? { renderValue } : {})} />
          </li>
        ))}
      </ol>
    );
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <span className="text-muted-foreground">{t("none")}</span>;
    return (
      <dl className={cn("grid gap-x-6 gap-y-2 sm:grid-cols-[minmax(8rem,14rem)_1fr]", className)}>
        {entries.slice(0, 200).map(([key, item]) => (
          <div key={key} className="contents">
            <dt className="text-sm text-muted-foreground">{humanizeKey(key)}</dt>
            <dd className="min-w-0 text-sm">
              {renderValue?.(key, item) ?? <JsonView value={item} depth={depth + 1} {...(renderValue ? { renderValue } : {})} />}
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <Scalar value={value} />;
}
