// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { useFormatter } from "next-intl";

type Formatter = ReturnType<typeof useFormatter>;

/** An amount in its currency for the viewer's locale (empty when there is none). */
export function money(format: Formatter, value: string | number | null | undefined, currency: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return String(value);
  if (!currency) return format.number(amount, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return format.number(amount, { style: "currency", currency, maximumFractionDigits: 2 });
}
