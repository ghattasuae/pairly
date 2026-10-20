// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SystemDashboard } from "@/lib/system-dashboards";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { SystemRoleDashboard } = await import("../home/system-dashboards");
const { OpsJobRuns } = await import("../home/ops-job-runs");

const TICKET = "22222222-2222-4222-8222-222222222222";

function governance(): SystemDashboard {
  return {
    board: "ai_governance",
    generated_at: "2026-10-16T08:00:00Z",
    action: [
      {
        key: "eval_gate_failing",
        count: 1,
        href: null,
        items: [{ kind: "eval_metric", id: "b.json:b.rate", label: "b.rate", href: null, at: null, status: "fail", detail: { why: "missing_file" } }],
        stats: [],
        source: "eval_result_files",
        note: null,
        workflow: null,
      },
    ],
    sections: [
      {
        key: "evals",
        count: 1,
        href: null,
        items: [
          {
            kind: "eval_file",
            id: "hire.json",
            label: "hire.json",
            href: null,
            at: "2026-10-01T10:00:00Z",
            status: "pass",
            detail: { mode: "recorded", pass: 12, fail: 0 },
          },
        ],
        stats: [
          { key: "gate_recorded", value: null, unit: "text", href: null, text: "fail", n: null },
          { key: "gate_live", value: null, unit: "text", href: null, text: null, n: null },
        ],
        source: "eval_result_files",
        note: null,
        workflow: null,
      },
      {
        key: "pii_redactions",
        count: null,
        href: null,
        items: [],
        stats: [{ key: "pii_redactions", value: null, unit: "count", href: null, text: null, n: 0 }],
        source: "pii_redaction_counts",
        note: "no_data",
        workflow: null,
      },
      {
        key: "human_overrides",
        count: 3,
        href: null,
        items: [],
        stats: [{ key: "override_rate_hire", value: 0.25, unit: "ratio", href: null, text: null, n: 12 }],
        source: "decision_records",
        note: null,
        workflow: null,
      },
    ],
    links: [{ key: "model_cards", href: "docs/model-cards/" }],
  };
}

describe("SystemRoleDashboard", () => {
  it("shows the gate, says when there is no data and never invents a number", () => {
    renderWithIntl(<SystemRoleDashboard dashboard="ai_governance" data={governance()} />);
    expect(screen.getByRole("heading", { name: "AI governance" })).toBeInTheDocument();
    expect(screen.getByText("1 thing needs you")).toBeInTheDocument();
    const evals = document.querySelector('[data-sys-section="evals"]') as HTMLElement;
    expect(within(evals).getByText("Gate, recorded mode")).toBeInTheDocument();
    expect(within(evals).getByText("Fail")).toBeInTheDocument();
    const live = evals.querySelector('[data-sys-stat="gate_live"]') as HTMLElement;
    expect(within(live).getByText("No data")).toBeInTheDocument();
    const pii = document.querySelector('[data-sys-section="pii_redactions"]') as HTMLElement;
    expect(pii.querySelector('[data-sys-count="none"]')).not.toBeNull();
    expect(within(pii).getByText("No data yet.")).toBeInTheDocument();
    expect(within(pii).queryByText("0")).toBeNull();
    const overrides = document.querySelector('[data-sys-stat="override_rate_hire"]') as HTMLElement;
    expect(overrides.textContent).toContain("25%");
    expect(overrides.textContent).toContain("of 12");
    // The model cards live in the repository: shown as a path, not a broken link.
    const cards = document.querySelector('[data-sys-link="model_cards"]') as HTMLElement;
    expect(cards.tagName).toBe("SPAN");
    expect(cards.textContent).toContain("docs/model-cards/");
  });

  it("lists each eval file once, live baselines included, with unique keys", () => {
    // The API used to list hire_live.json and pay_live.json twice (as a file the thresholds
    // name and as a recorded file's live companion), so React saw duplicate keys on Home.
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const data = governance();
    const file = (id: string, mode: string) => ({
      kind: "eval_file",
      id,
      label: id,
      href: null,
      at: "2026-10-17T10:00:00Z",
      status: "pass",
      detail: { mode, pass: 3, fail: 0 },
    });
    data.sections[0]!.items = [
      file("hire.json", "recorded"),
      file("hire_live.json", "live"),
      file("pay.json", "recorded"),
      file("pay_live.json", "live"),
    ];
    renderWithIntl(<SystemRoleDashboard dashboard="ai_governance" data={data} />);
    const evals = document.querySelector('[data-sys-section="evals"]') as HTMLElement;
    const rows = Array.from(evals.querySelectorAll('[data-sys-item="eval_file"]'));
    expect(rows.map((r) => r.textContent?.match(/\w+(_live)?\.json/)?.[0])).toEqual([
      "hire.json",
      "hire_live.json",
      "pay.json",
      "pay_live.json",
    ]);
    const keyWarnings = errors.mock.calls.filter((call) => String(call[0]).includes("same key"));
    expect(keyWarnings).toEqual([]);
    errors.mockRestore();
  });

  it("links every count and record to its page", () => {
    const board: SystemDashboard = {
      board: "auditor",
      generated_at: "2026-10-16T08:00:00Z",
      action: [],
      sections: [
        {
          key: "bank_changes",
          count: 4,
          href: "/pay/bank-changes",
          items: [
            {
              kind: "bank_change",
              id: TICKET,
              label: "BCT-0001",
              href: `/pay/bank-changes/${TICKET}`,
              at: "2026-10-15T08:00:00Z",
              status: "awaiting_approval",
              detail: { verified: true, called_last4: "0042", last_call: "confirmed" },
            },
          ],
          stats: [],
          source: null,
          note: null,
          workflow: "pay",
        },
        {
          key: "plugin_added_key",
          count: 0,
          href: null,
          items: [],
          stats: [],
          source: null,
          note: null,
          workflow: "expense",
        },
      ],
      links: [{ key: "audit_search", href: "/audit" }],
    };
    renderWithIntl(<SystemRoleDashboard dashboard="auditor" data={board} />);
    expect(screen.getByText("Nothing needs your attention here.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "4 Bank-detail changes and their verification: open the records" })).toHaveAttribute(
      "href",
      "/pay/bank-changes",
    );
    expect(screen.getByRole("link", { name: "BCT-0001" })).toHaveAttribute("href", `/pay/bank-changes/${TICKET}`);
    expect(screen.getByText(/number called ends in: 0042/)).toBeInTheDocument();
    expect(screen.getByText(/verified: yes/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See all 4" })).toBeInTheDocument();
    // An unknown key still reads.
    expect(screen.getByText("plugin added key")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Search the audit log/ })).toHaveAttribute("href", "/audit");
  });

  it("reads in Arabic", () => {
    renderWithIntl(<SystemRoleDashboard dashboard="ai_governance" data={governance()} />, "ar");
    expect(screen.getByRole("heading", { name: "حوكمة الذكاء الاصطناعي" })).toBeInTheDocument();
    expect(screen.getAllByText("لا توجد بيانات").length).toBeGreaterThan(0);
    expect(screen.getByText("التقييمات مقابل حدودها")).toBeInTheDocument();
  });
});

describe("OpsJobRuns", () => {
  it("lists runs with their status, error and summary", () => {
    renderWithIntl(
      <OpsJobRuns
        runs={[
          {
            id: "33333333-3333-4333-8333-333333333333",
            job_name: "batch_url_sweep",
            started_at: "2026-10-16T08:00:00Z",
            finished_at: null,
            status: "running",
            summary: null,
            error: null,
          },
          {
            id: "44444444-4444-4444-8444-444444444444",
            job_name: "deliver_webhooks",
            started_at: "2026-10-16T07:00:00Z",
            finished_at: "2026-10-16T07:00:01Z",
            status: "failed",
            summary: { sent: 3 },
            error: "TimeoutError",
          },
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "batch_url_sweep" })).toHaveAttribute("href", "/ops/jobs?job=batch_url_sweep");
    expect(screen.getByText("still running", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("TimeoutError")).toBeInTheDocument();
    expect(screen.getByText(/sent: 3/)).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("says when there is nothing yet", () => {
    renderWithIntl(<OpsJobRuns runs={[]} />);
    expect(screen.getByText("No runs recorded yet.")).toBeInTheDocument();
  });
});
