// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { isLocale } from "@/i18n/routing";

const SEGMENT = /^[A-Za-z0-9_-]{1,128}$/;

/**
 * A same-site page path to continue to after sign-in, or null.
 *
 * Only `/<locale>` and `/<locale>/<segment>/...` with plain segments pass, so the value can
 * never point to another site (`//evil`, `/\evil`, `https:`), carry a query or fragment,
 * or climb with `..`.
 */
export function safeNextPath(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 512) return null;
  const parts = value.split("/");
  if (parts[0] !== "") return null;
  const [, locale, ...rest] = parts;
  if (!isLocale(locale)) return null;
  if (rest.length === 1 && rest[0] === "") return `/${locale}`;
  if (!rest.every((segment) => SEGMENT.test(segment))) return null;
  return ["", locale, ...rest].join("/");
}
