// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { allTags, boardQuery, columns, EMPTY_STATE, filterCards, nextStage, parseBoardState } from "./board";
import type { HireCard, HireStage } from "./types";

const STAGES: HireStage[] = [
  { key: "applied", label: "Applied", label_ar: "تقدم", kind: "active", reveal_identity: false },
  { key: "screening", label: "Screening", label_ar: "الفرز", kind: "active", reveal_identity: false },
  { key: "offer", label: "Offer", label_ar: "العرض", kind: "active", reveal_identity: true },
  { key: "hired", label: "Hired", label_ar: "تم التوظيف", kind: "hired", reveal_identity: true },
  { key: "rejected", label: "Rejected", label_ar: "مرفوض", kind: "rejected", reveal_identity: false },
];

function card(n: number, patch: Partial<HireCard> = {}): HireCard {
  return {
    application_id: `0000000${n}-0000-4000-8000-000000000000`,
    reference: `REF${n}-AAAA`,
    label: `Person ${n}`,
    identity_hidden: false,
    stage_key: "applied",
    stage_entered_at: "2026-10-01T10:00:00Z",
    submitted_at: "2026-10-01T10:00:00Z",
    waiting_days: 1,
    sla_overdue: false,
    tags: [],
    needs_human_review: false,
    review_reasons: [],
    duplicate_hints: 0,
    referred: false,
    open_message: null,
    open_message_id: null,
    ...patch,
  };
}

describe("board state in the URL", () => {
  it("round-trips filters, the open candidate and the scroll position", () => {
    const state = {
      q: "python",
      tag: "remote",
      flag: "waiting" as const,
      candidate: "11111111-1111-4111-8111-111111111111",
      x: 640,
      y: 120,
    };
    const query = boardQuery(state);
    expect(query).toBe(
      "?q=python&tag=remote&flag=waiting&candidate=11111111-1111-4111-8111-111111111111&x=640&y=120",
    );
    expect(parseBoardState(Object.fromEntries(new URLSearchParams(query)))).toEqual(state);
    expect(boardQuery(EMPTY_STATE)).toBe("");
  });

  it("drops anything malformed", () => {
    expect(
      parseBoardState({ candidate: "../etc", flag: "everything", tag: "<script>", x: "-5", y: "huge" }),
    ).toEqual(EMPTY_STATE);
  });
});

describe("cards", () => {
  const cards = [
    card(1, { tags: ["Python"], sla_overdue: true }),
    card(2, { stage_key: "screening", needs_human_review: true }),
    card(3, { stage_key: "unknown_stage", referred: true, tags: ["remote"] }),
  ];

  it("filters by text, tag and flag", () => {
    expect(filterCards(cards, { q: "person 2", tag: null, flag: null }).map((c) => c.label)).toEqual(["Person 2"]);
    expect(filterCards(cards, { q: "", tag: "python", flag: null }).map((c) => c.label)).toEqual(["Person 1"]);
    expect(filterCards(cards, { q: "", tag: null, flag: "waiting" })).toHaveLength(1);
    expect(filterCards(cards, { q: "", tag: null, flag: "needs_review" })).toHaveLength(1);
    expect(filterCards(cards, { q: "", tag: null, flag: "referred" })).toHaveLength(1);
    expect(filterCards(cards, { q: "ref2", tag: null, flag: null })).toHaveLength(1);
  });

  it("groups by stage and keeps every column; unknown stages land in the first", () => {
    const grouped = columns(STAGES, cards);
    expect([...grouped.keys()]).toEqual(["applied", "screening", "offer", "hired", "rejected"]);
    expect(grouped.get("applied")?.map((c) => c.label)).toEqual(["Person 1", "Person 3"]);
    expect(grouped.get("offer")).toEqual([]);
    expect(allTags(cards)).toEqual(["Python", "remote"]);
  });

  it("knows the next stage like the API does", () => {
    expect(nextStage(STAGES, "applied")?.key).toBe("screening");
    expect(nextStage(STAGES, "offer")?.key).toBe("hired");
    expect(nextStage(STAGES, "hired")).toBeNull();
    expect(nextStage(STAGES, "rejected")).toBeNull();
  });
});
