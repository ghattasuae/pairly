// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** The exceptions queue (brief 4.5.4): categories, filtering, keyboard moves and age, pure. */
import type { PayExceptionItem } from "./types";

export const EXCEPTION_CATEGORIES = [
  "no_po",
  "price_variance",
  "quantity_variance",
  "missing_receipt",
  "duplicate_suspect",
  "bank_mismatch",
  "policy_breach",
  "low_confidence",
  "other",
] as const;

export type ExceptionCategoryKey = (typeof EXCEPTION_CATEGORIES)[number];

export function isCategory(value: string | null | undefined): value is ExceptionCategoryKey {
  return !!value && (EXCEPTION_CATEGORIES as readonly string[]).includes(value);
}

/** The items of one category (all of them without one). */
export function itemsOf(items: readonly PayExceptionItem[], category: ExceptionCategoryKey | null): PayExceptionItem[] {
  if (!category) return [...items];
  return items.filter((item) => item.categories.some((c) => c.category === category));
}

/**
 * The selected row after a key: J (or the down arrow) moves down, K (or up) moves up,
 * staying inside the list. Any other key changes nothing. -1 when the list is empty.
 */
export function move(index: number, key: string, length: number): number {
  if (length <= 0) return -1;
  const current = Math.min(Math.max(index, 0), length - 1);
  if (key === "j" || key === "J" || key === "ArrowDown") return Math.min(current + 1, length - 1);
  if (key === "k" || key === "K" || key === "ArrowUp") return Math.max(current - 1, 0);
  return current;
}

/** Waiting time as days and hours (for "2 d 5 h"). */
export function ageParts(hours: number): { days: number; hours: number } {
  const whole = Math.max(0, Math.floor(hours));
  return { days: Math.floor(whole / 24), hours: whole % 24 };
}

/** Whether a key press belongs to a text field (then the list shortcuts stay off). */
export function typingIn(target: EventTarget | null): boolean {
  if (!target || typeof (target as HTMLElement).tagName !== "string") return false;
  const element = target as HTMLElement;
  const tag = element.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || element.isContentEditable === true;
}
