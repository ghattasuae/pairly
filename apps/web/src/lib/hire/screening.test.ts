// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import {
  type CandidateDetail,
  DetailCache,
  filterQueue,
  neighbour,
  parseScreenState,
  prefetchIds,
  type QueueItem,
  screenQuery,
  splitText,
  toggleCompare,
  whyNotHigher,
  withTag,
} from "./screening";

const item = (n: number, patch: Partial<QueueItem> = {}): QueueItem => ({
  application_id: `0000000${n}-0000-4000-8000-000000000000`,
  reference: `REF${n}`,
  label: `Candidate REF${n}`,
  identity_hidden: true,
  stage_key: "applied",
  stage_kind: "active",
  tags: [],
  screening_status: null,
  scored: false,
  overall_score: null,
  flag_codes: [],
  needs_human_review: false,
  open_message: null,
  open_message_id: null,
  submitted_at: null,
  ...patch,
});

const items = [item(1), item(2, { tags: ["fintech"] }), item(3), item(4)];
const ids = items.map((i) => i.application_id);

describe("screening mode helpers", () => {
  it("keeps the candidate and the search in the URL, safely", () => {
    expect(parseScreenState({ candidate: ids[1], q: "fin" })).toEqual({ candidate: ids[1], q: "fin" });
    expect(parseScreenState({ candidate: "../../etc", q: undefined })).toEqual({ candidate: null, q: "" });
    expect(screenQuery({ candidate: ids[0]!, q: "a b" })).toBe(`?candidate=${ids[0]}&q=a+b`);
    expect(screenQuery({ candidate: null, q: "" })).toBe("");
  });

  it("moves through the queue without jumping past its ends", () => {
    expect(neighbour(items, ids[0]!, 1)).toBe(ids[1]);
    expect(neighbour(items, ids[0]!, -1)).toBe(ids[0]);
    expect(neighbour(items, ids[3]!, 1)).toBe(ids[3]);
    expect(neighbour(items, "missing", 1)).toBe(ids[0]);
    expect(neighbour([], ids[0]!, 1)).toBeNull();
  });

  it("prefetches the next two and the previous one", () => {
    expect(prefetchIds(items, ids[1]!)).toEqual([ids[2], ids[3], ids[0]]);
    expect(prefetchIds(items, ids[3]!)).toEqual([ids[2]]);
    expect(prefetchIds(items, null)).toEqual([]);
  });

  it("searches labels, references and tags", () => {
    expect(filterQueue(items, "FINTECH").map((i) => i.reference)).toEqual(["REF2"]);
    expect(filterQueue(items, " ref3 ").map((i) => i.reference)).toEqual(["REF3"]);
    expect(filterQueue(items, "")).toHaveLength(4);
  });

  it("compares at most three candidates", () => {
    let chosen: string[] = [];
    for (const id of ids) chosen = toggleCompare(chosen, id);
    expect(chosen).toEqual(ids.slice(1));
    expect(toggleCompare(chosen, ids[2]!)).toEqual([ids[1], ids[3]]);
  });

  it("explains why a score is not higher from the rubric's anchors", () => {
    const base = { score: 2, flags: [] as string[], evidence: [{}] as never[], next_anchor: "Leads a team." };
    expect(whyNotHigher(base, 4)).toEqual({ key: "nextLevel", values: { score: 2, max: 4, next: "Leads a team." } });
    expect(whyNotHigher({ ...base, score: 4 }, 4).key).toBe("top");
    expect(whyNotHigher({ ...base, evidence: [] }, 4).key).toBe("noEvidence");
    expect(whyNotHigher({ ...base, flags: ["not_scored"] }, 4).key).toBe("notScored");
    expect(whyNotHigher({ ...base, next_anchor: null }, 4).key).toBe("generic");
  });

  it("splits the blind text into marks and labelled hidden parts without changing it", () => {
    const text = "[name] wrote Go at [university] for years.";
    const pieces = splitText(text, [{ id: "ev-1", start: 7, end: 15 }, { id: "bad", start: 30, end: 999 }]);
    expect(pieces.map((p) => p.text).join("")).toBe(text);
    expect(pieces.filter((p) => p.hidden).map((p) => p.text)).toEqual(["[name]", "[university]"]);
    expect(pieces.find((p) => p.evidence.includes("ev-1"))?.text).toBe("wrote Go");
    expect(pieces.some((p) => p.evidence.includes("bad"))).toBe(false);
  });

  it("caches candidate details, newest kept", () => {
    const cache = new DetailCache(2);
    const d = (n: number) => ({ screening: null, profile: { application_id: `${n}` } }) as unknown as CandidateDetail;
    cache.set("a", d(1));
    cache.set("b", d(2));
    cache.get("a");
    cache.set("c", d(3));
    expect(cache.has("a")).toBe(true);
    expect(cache.has("b")).toBe(false);
    expect(cache.peek("c")).toBeDefined();
  });

  it("adds the hold tag once", () => {
    expect(withTag(["x"], "On hold")).toEqual(["x", "On hold"]);
    expect(withTag(["on hold"], "On hold")).toEqual(["on hold"]);
  });
});
