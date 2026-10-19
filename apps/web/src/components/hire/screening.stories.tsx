// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useState } from "react";

import { Kbd } from "@/components/ui/kbd";
import { evidenceId, type ScreeningPanel } from "@/lib/hire/screening";

import { ScorePanel } from "./score-panel";
import { ScreeningDocument } from "./screening-document";

const TEXT = [
  "Summary",
  "[name] led month-end close for a 40-entity group at [employer], reconciling intercompany balances.",
  "Prepared consolidated statements under IFRS 16 and trained four junior accountants.",
].join("\n");

function range(quote: string) {
  const start = TEXT.indexOf(quote);
  return { quote, start: 0, end: quote.length, page: null, bboxes: [], display_start: start, display_end: start + quote.length };
}

const SCREENING: ScreeningPanel = {
  id: "11111111-0000-4000-8000-000000000001",
  application_id: "11111111-0000-4000-8000-000000000002",
  job_id: "11111111-0000-4000-8000-000000000003",
  status: "awaiting_review",
  run_id: null,
  run_status: null,
  blind: true,
  blind_config: { enabled: true },
  rubric_policy_id: "22222222-2222-4222-8222-222222222222",
  rubric_version: 1,
  scale: { min: 0, max: 4 },
  scored: true,
  overall_score: 78,
  ai_overall_score: 78,
  criteria: [
    {
      criterion_id: "close",
      name: "Month-end close",
      weight: 5,
      must_have: true,
      score: 3,
      model_score: 3,
      confidence: 0.9,
      evidence: [range("led month-end close for a 40-entity group")],
      rejected_quotes: 0,
      why_not_higher: "No lead role over a team shown.",
      flags: [],
      overridden: false,
      anchor: "Runs the close end to end.",
      next_anchor: "Leads the close for a large group.",
    },
    {
      criterion_id: "ifrs",
      name: "IFRS reporting",
      weight: 3,
      must_have: false,
      score: 2,
      model_score: 2,
      confidence: 0.7,
      evidence: [range("Prepared consolidated statements under IFRS 16")],
      rejected_quotes: 0,
      why_not_higher: "",
      flags: [],
      overridden: true,
      anchor: "Prepares statements.",
      next_anchor: "Owns the IFRS policy.",
    },
  ],
  flags: [{ code: "unexplained_gap", kind: "claim", severity: "info", explanation: "gap", details: {} }],
  guardrail_findings: [],
  knockouts: [],
  review_reasons: [],
  models: {},
  profile: null,
  text: TEXT,
  document_id: null,
  approval_id: null,
  score_decision_id: null,
  decision_ids: [],
  overrides: [],
  failure_reason: null,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
  decided_at: null,
};

/**
 * Screening mode's split view: the blind CV on the start side with evidence marked, the
 * criteria, scores and quotes on the end side. Selecting a quote marks it in the CV.
 */
function SplitView() {
  const [active, setActive] = useState<{ id: string; seq: number } | null>({ id: evidenceId("close", 0), seq: 1 });
  return (
    <div className="space-y-3 p-6">
      <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
        {["J", "K", "A", "H", "R", "N", "S", "T"].map((key) => (
          <Kbd key={key}>{key}</Kbd>
        ))}
      </div>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <ScreeningDocument screening={SCREENING} active={active} />
        <ScorePanel
          screening={SCREENING}
          activeEvidence={active?.id ?? null}
          canOverride
          onQuote={(id) => setActive((a) => ({ id, seq: (a?.seq ?? 0) + 1 }))}
          onOverride={() => Promise.resolve(true)}
        />
      </div>
    </div>
  );
}

const meta = { title: "Hire/Screening split view", component: SplitView } satisfies Meta<typeof SplitView>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Light: Story = {};
export const Dark: Story = { globals: { theme: "dark" } };
export const Arabic: Story = { globals: { locale: "ar" } };
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
