// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { defineRouting } from "next-intl/routing";

export const locales = ["en", "ar"] as const;
export type Locale = (typeof locales)[number];

export const defaultLocale: Locale = "en";

/** Text direction per locale. Arabic is right to left. */
export const localeDirection: Record<Locale, "ltr" | "rtl"> = { en: "ltr", ar: "rtl" };

export const routing = defineRouting({
  locales,
  defaultLocale,
  // Every URL carries its locale (/en/..., /ar/...), so links and emails are unambiguous.
  localePrefix: "always",
  localeCookie: { name: "pairly_locale", sameSite: "lax", maxAge: 60 * 60 * 24 * 365 },
});

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (locales as readonly string[]).includes(value);
}

/** Narrow a route param. Unknown values were already sent to 404 by the locale layout. */
export function toLocale(value: string): Locale {
  return isLocale(value) ? value : defaultLocale;
}
