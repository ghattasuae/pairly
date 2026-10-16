// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";

export const MAIN_CONTENT_ID = "main-content";

/** First focusable element on every page: jumps past the navigation. */
export function SkipLink() {
  const t = useTranslations("Common");
  return (
    <a
      href={`#${MAIN_CONTENT_ID}`}
      className="sr-only z-[100] rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:start-4 focus:top-4"
    >
      {t("skipToContent")}
    </a>
  );
}
