// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Wrap a left-to-right value (email, id, URL) in Unicode first-strong isolate marks so it
 * renders correctly inside Arabic sentences. Use for values interpolated into messages.
 */
export function isolate(value: string): string {
  return `⁨${value}⁩`;
}
