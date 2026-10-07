// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { cleanEvidence, isExplanationKey } from "@/components/data/explain";
import { hitHref } from "./search-links";
import { minutesToTime, snoozeEnd, timeToMinutes } from "./notification-prefs";

import { readLinkToken } from "./approval-link-token";
import { auditApiQuery, auditQuery, parseAuditCursor, parseAuditFilters } from "./audit-filters";
import { isDecisionId } from "./decision-id";
import { highlightsFromFields, highlightsFromFindings, readContext, toBox } from "./evidence";
import { inAppPath } from "./in-app-link";
import {
  apiQuery,
  approvalWorkflows,
  needsConfirm,
  needsReason,
  newIdempotencyKey,
  parseFilters,
  parseItem,
  toQuery,
} from "./inbox-filters";
import { canSee } from "./settings-pages";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const TOKEN = "A".repeat(20) + "b_-".repeat(7) + "z".repeat(2);

describe("inbox filters", () => {
  it("parses known values and drops the rest", () => {
    const f = parseFilters(new URLSearchParams("workflow=pay&status=rejected&sla=overdue&mine=1&q=rent"));
    expect(f).toEqual({ workflow: "pay", status: "rejected", sla: "overdue", mine: true, q: "rent" });
    expect(parseFilters({ workflow: "Pay!", status: "nope", sla: "late" })).toEqual({
      workflow: null,
      status: "pending",
      sla: null,
      mine: false,
      q: "",
    });
  });

  it("round-trips through the URL, leaving defaults out", () => {
    const f = parseFilters({ status: "all", mine: "1" });
    expect(toQuery(f, ID)).toBe(`?status=all&mine=1&item=${ID}`);
    expect(toQuery(parseFilters({}), null)).toBe("");
    expect(parseItem({ item: ID.toUpperCase() })).toBe(ID);
    expect(parseItem({ item: "../etc" })).toBeNull();
  });

  it("builds the API query", () => {
    expect(apiQuery(parseFilters({ status: "all", sla: "due_soon", mine: "1" }), "c1", 20)).toEqual({
      sla: "due_soon",
      assigned_to_me: true,
      cursor: "c1",
      limit: 20,
    });
    expect(apiQuery(parseFilters({}), null)).toEqual({ status: "pending", limit: 50 });
  });

  it("knows which decisions need a reason and a confirmation", () => {
    expect(needsReason("approve")).toBe(false);
    expect(needsReason("reject")).toBe(true);
    expect(needsReason("request_changes")).toBe(true);
    expect(needsConfirm("approve")).toBe(true);
    expect(needsConfirm("reject")).toBe(true);
    expect(needsConfirm("request_changes")).toBe(false);
  });

  it("makes idempotency keys the API accepts, never the same twice", () => {
    const a = newIdempotencyKey();
    expect(a).toMatch(/^[A-Za-z0-9._:-]{8,128}$/);
    expect(newIdempotencyKey()).not.toBe(a);
  });

  it("lists workflows from approve permissions only", () => {
    expect(approvalWorkflows(["pay.invoice.approve", "hire.offer.approve", "core.x.approve", "pay.invoice.read"])).toEqual([
      "hire",
      "pay",
    ]);
  });
});

describe("audit filters", () => {
  it("accepts plain values and whole UTC days", () => {
    const f = parseAuditFilters({ action: "core.approval.decided", actor_type: "user", from: "2026-10-01", to: "2026-10-07" });
    expect(auditApiQuery(f)).toEqual({
      action: "core.approval.decided",
      actor_type: "user",
      from: "2026-10-01T00:00:00Z",
      to: "2026-10-08T00:00:00.000Z",
    });
    expect(auditQuery(f, { cursor: "abc" })).toBe(
      "?action=core.approval.decided&actor_type=user&from=2026-10-01&to=2026-10-07&cursor=abc",
    );
  });

  it("drops anything malformed", () => {
    const f = parseAuditFilters({ action: "<script>", actor_type: "root", from: "2026-02-30", workflow: "Pay" });
    expect(f.action).toBe("");
    expect(f.actorType).toBe("");
    expect(f.from).toBe("");
    expect(f.workflow).toBe("");
    expect(parseAuditCursor({ cursor: "a/b" })).toBeNull();
  });
});

describe("evidence highlights", () => {
  it("keeps only well-formed boxes and pages", () => {
    expect(toBox({ x0: 1, top: 2, x1: 3, bottom: 4 })).toEqual({ x0: 1, top: 2, x1: 3, bottom: 4 });
    expect(toBox({ x0: 3, top: 2, x1: 1, bottom: 4 })).toBeNull();
    expect(toBox({ x0: "1", top: 2, x1: 3, bottom: 4 })).toBeNull();
    const [h] = highlightsFromFields([
      { path: "total", spans: [{ page: 2, quote: "AED 10", bboxes: [{ x0: 1, top: 1, x1: 9, bottom: 4 }, null] }] },
    ]);
    expect(h).toMatchObject({ kind: "evidence", page: 2, label: "total", quote: "AED 10" });
    expect(h?.boxes).toHaveLength(1);
  });

  it("turns guardrail findings into explained highlights", () => {
    const [f] = highlightsFromFindings([
      { id: "g1", type: "hidden_text", code: "tiny_font", severity: "high", page: 1, bbox: { x0: 0, top: 0, x1: 5, bottom: 5 }, evidence: "ignore" },
    ]);
    expect(f).toMatchObject({ kind: "guardrail", explanationKey: "guardrails.hidden_text.tiny_font", severity: "high" });
    expect(highlightsFromFindings("nope")).toEqual([]);
  });

  it("reads the approval context convention", () => {
    const ctx = readContext({
      document_id: ID,
      evidence: [{ label: "Amount", quote: "120", page: 1, bbox: { x0: 1, top: 1, x1: 2, bottom: 2 } }],
      decision_ids: ["dec_01J9Z3K6Y8W2X4V5T7R9Q1P3N5", "not-an-id"],
    });
    expect(ctx.documentId).toBe(ID);
    expect(ctx.highlights).toHaveLength(1);
    expect(ctx.decisionIds).toEqual(["dec_01J9Z3K6Y8W2X4V5T7R9Q1P3N5"]);
    expect(readContext({ document_id: "../x" }).documentId).toBeNull();
  });
});

describe("links and tokens", () => {
  it("follows only in-app paths from notifications", () => {
    expect(inAppPath("/en/inbox?item=abc")).toBe("/en/inbox?item=abc");
    expect(inAppPath("http://localhost:3100/ar/inbox")).toBe("/ar/inbox");
    expect(inAppPath("https://evil.example/en/inbox")).toBe("/en/inbox");
    expect(inAppPath("//evil.example")).toBeNull();
    expect(inAppPath("javascript:alert(1)")).toBeNull();
    expect(inAppPath("/xx/inbox")).toBeNull();
    expect(inAppPath("/en/../admin")).toBeNull();
    expect(inAppPath("/en/inbox?<script>")).not.toContain("<");
  });

  it("reads approval link tokens from the fragment in both forms", () => {
    expect(TOKEN).toHaveLength(43);
    expect(readLinkToken(`#${TOKEN}`)).toBe(TOKEN);
    expect(readLinkToken(`#token=${TOKEN}`)).toBe(TOKEN);
    expect(readLinkToken("#short")).toBeNull();
    expect(readLinkToken(`#${TOKEN}x`)).toBeNull();
  });

  it("checks decision ids", () => {
    expect(isDecisionId("dec_01J9Z3K6Y8W2X4V5T7R9Q1P3N5")).toBe(true);
    expect(isDecisionId("dec_01j9z3k6y8w2x4v5t7r9q1p3n5")).toBe(false);
    expect(isDecisionId("dec_../")).toBe(false);
  });

  it("sends search hits to the right page", () => {
    expect(hitHref({ kind: "document", source_id: ID, page_start: 3 })).toBe(`/documents/${ID}?page=3`);
    expect(hitHref({ kind: "policy", source_id: ID, page_start: null })).toBe(`/settings/policies#policy-${ID}`);
    expect(hitHref({ kind: "other", source_id: ID, page_start: null })).toBeNull();
    expect(hitHref({ kind: "document", source_id: "../../x", page_start: null })).toBeNull();
  });
});

describe("explanations", () => {
  it("cleans evidence like the API does", () => {
    expect(cleanEvidence('say "yes"​ now')).toBe("say 'yes' now");
    expect(cleanEvidence("a".repeat(100))).toHaveLength(80);
    expect(isExplanationKey("guardrails.hidden_text.tiny_font")).toBe(true);
    expect(isExplanationKey("__proto__")).toBe(false);
  });
});

describe("notification preferences helpers", () => {
  it("converts quiet hours", () => {
    expect(minutesToTime(18 * 60 + 5)).toBe("18:05");
    expect(timeToMinutes("08:30")).toBe(510);
    expect(timeToMinutes("25:00")).toBeNull();
  });

  it("ends a snooze at the right time", () => {
    const now = new Date(2026, 9, 8, 15, 0, 0);
    expect(snoozeEnd("1h", now).getHours()).toBe(16);
    const tomorrow = snoozeEnd("tomorrow", now);
    expect([tomorrow.getDate(), tomorrow.getHours()]).toEqual([9, 9]);
  });
});

describe("settings pages", () => {
  it("shows organisation pages only to who holds their permission", () => {
    expect(canSee("notifications", [])).toBe(true);
    expect(canSee("flags", [])).toBe(false);
    expect(canSee("flags", ["core.flags.read"])).toBe(true);
    expect(canSee("apiKeys", ["core.api_keys.manage"])).toBe(true);
  });
});
