// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import {
  detailEntries,
  formatBytes,
  quietlyAbsent,
  safeHref,
  statValue,
  systemActionTotal,
  systemDashboardsFor,
  type SystemDashboard,
} from "./system-dashboards";

const ALL = ["core.audit.read", "core.admin_dashboard.read", "core.ai_governance.read", "core.platform.read"];

describe("systemDashboardsFor", () => {
  it("gives auditors the audit and AI governance boards", () => {
    expect(systemDashboardsFor([{ role: "auditor" }], ["core.audit.read", "core.ai_governance.read"])).toEqual([
      "auditor",
      "ai_governance",
    ]);
  });

  it("gives admins the admin, AI governance and platform boards, in a stable order", () => {
    expect(systemDashboardsFor([{ role: "admin", workflow: null }], ALL)).toEqual(["admin", "ai_governance", "platform_ops"]);
    expect(systemDashboardsFor([{ role: "admin" }, { role: "auditor" }], ALL)).toEqual([
      "auditor",
      "admin",
      "ai_governance",
      "platform_ops",
    ]);
  });

  it("ignores workflow-scoped memberships and missing permissions", () => {
    expect(systemDashboardsFor([{ role: "admin", workflow: "pay" }], ALL)).toEqual([]);
    expect(systemDashboardsFor([{ role: "auditor" }], ["core.audit.read"])).toEqual(["auditor"]);
    expect(systemDashboardsFor([{ role: "recruiter" }], ALL)).toEqual([]);
  });
});

describe("helpers", () => {
  it("leaves platform operations out quietly on 404 only", () => {
    expect(quietlyAbsent("platform_ops", 404)).toBe(true);
    expect(quietlyAbsent("platform_ops", 500)).toBe(false);
    expect(quietlyAbsent("admin", 404)).toBe(false);
  });

  it("never turns a missing number into 0", () => {
    expect(statValue({ key: "x", value: null, unit: "count", href: null, text: null, n: null })).toEqual({ kind: "none" });
    expect(statValue({ key: "x", value: null, unit: "text", href: null, text: "pass", n: null })).toEqual({ kind: "text", text: "pass" });
    expect(statValue({ key: "x", value: null, unit: "text", href: null, text: null, n: null })).toEqual({ kind: "none" });
    expect(statValue({ key: "x", value: 0.25, unit: "ratio", href: null, text: null, n: 4 })).toEqual({
      kind: "number",
      value: 0.25,
      unit: "ratio",
    });
  });

  it("counts what waits and skips unknown counts", () => {
    const board = (counts: (number | null)[]): SystemDashboard => ({
      board: "admin",
      generated_at: "2026-10-16T08:00:00Z",
      action: counts.map((count, i) => ({
        key: `k${i}`,
        count,
        href: null,
        items: [],
        stats: [],
        source: null,
        note: null,
        workflow: null,
      })),
      sections: [],
      links: [],
    });
    expect(systemActionTotal([board([2, null]), board([1])])).toBe(3);
  });

  it("formats bytes and keeps only app paths", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GiB");
    expect(safeHref("/audit")).toBe("/audit");
    expect(safeHref("//evil.example")).toBeNull();
    expect(safeHref("docs/model-cards/")).toBeNull();
    expect(detailEntries({ a: 1, b: null, c: "", d: false })).toEqual([
      ["a", 1],
      ["d", false],
    ]);
  });
});
