// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { logAxe } from "./report";

/** Run axe-core on the current page and log every violation (rule id, impact, selectors). */
export async function auditA11y(page: Page, pageName: string, locale: string, theme: string): Promise<number> {
  const results = await new AxeBuilder({ page }).analyze();
  const violations = results.violations.map((v) => ({
    id: v.id,
    impact: v.impact ?? null,
    help: v.help,
    nodes: v.nodes.map((n) => n.target.join(" ")),
  }));
  logAxe({ page: pageName, locale, theme, violations });
  return violations.length;
}
