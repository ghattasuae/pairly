// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { checkApplyRequest, isUploadToken, knownCode, parseApplyBody } from "./apply";
import { clientIpFrom, descriptionBlocks, isSlug, jsonLdScript, readPublicForm, salaryRange } from "./format";

const EM_DASH = String.fromCharCode(0x2014);

describe("jsonLdScript", () => {
  it("stays valid JSON and can never close the script element", () => {
    const data = { "@type": "JobPosting", title: "</script><script>alert(1)</script>", note: `a & b ${String.fromCharCode(0x2028)}` };
    const out = jsonLdScript(data);
    expect(out).not.toContain("</script");
    expect(out).not.toContain("<");
    expect(out).not.toContain("&");
    expect(JSON.parse(out)).toEqual(data);
  });
});

describe("descriptionBlocks", () => {
  it("turns plain job text into headings, paragraphs and lists", () => {
    const blocks = descriptionBlocks("About the role\nYou will build services.\n\nWhat you need\n- Python\n- SQL\nThanks.");
    expect(blocks).toEqual([
      { kind: "heading", text: "About the role" },
      { kind: "paragraph", text: "You will build services." },
      { kind: "heading", text: "What you need" },
      { kind: "list", items: ["Python", "SQL"] },
      { kind: "paragraph", text: "Thanks." },
    ]);
  });

  it("handles Arabic text", () => {
    const blocks = descriptionBlocks("عن الوظيفة\nستبني خدمات.\n\nما تحتاجه\n- خبرة");
    expect(blocks[0]).toEqual({ kind: "heading", text: "عن الوظيفة" });
    expect(blocks.at(-1)).toEqual({ kind: "list", items: ["خبرة"] });
  });
});

describe("salaryRange", () => {
  it("formats a shown range and hides a missing one", () => {
    expect(salaryRange({ min: "30000.00", max: "38000.00", currency: "AED", period: "month" }, "en")).toEqual({
      min: "30,000",
      max: "38,000",
      currency: "AED",
    });
    expect(salaryRange(null, "en")).toBeNull();
    expect(salaryRange({ min: "x", max: "1", currency: "AED", period: "year" }, "en")).toBeNull();
  });
});

describe("readPublicForm", () => {
  it("keeps well formed parts and drops anything else", () => {
    const form = readPublicForm({
      standard: { phone: "required", location: "hidden", profile_url: "weird" },
      questions: [
        { id: "notice", kind: "text", label: "Notice", required: true, max_length: 200 },
        { id: "Bad Id", kind: "text", label: "x" },
        { id: "level", kind: "choice", label: "Level", options: [{ id: "mid", label: "Mid" }, { id: "<b>", label: "x" }] },
        { id: "kind", kind: "script", label: "x" },
      ],
      knockout_questions: [{ id: "right_to_work", question: "Right to work?", question_ar: "حق العمل؟" }],
      files: [{ id: "cv", label: "CV", label_ar: "", required: true }],
      self_identification: [{ id: "gender", label: "Gender", label_ar: "الجنس", options: [{ id: "woman", label: "Woman" }] }],
    });
    expect(form.standard).toEqual({ phone: "required", location: "hidden", profile_url: "optional" });
    expect(form.questions.map((q) => q.id)).toEqual(["notice", "level"]);
    expect(form.questions[1]?.options.map((o) => o.id)).toEqual(["mid"]);
    expect(form.knockout_questions[0]?.question_ar).toBe("حق العمل؟");
    expect(form.files[0]?.required).toBe(true);
    expect(form.self_identification[0]?.options).toHaveLength(1);
    expect(readPublicForm(null).questions).toEqual([]);
  });
});

describe("clientIpFrom", () => {
  it("prefers X-Real-IP, else the last X-Forwarded-For hop, else nothing", () => {
    expect(clientIpFrom(new Headers({ "x-real-ip": "198.51.100.4" }))).toBe("198.51.100.4");
    expect(clientIpFrom(new Headers({ "x-forwarded-for": "1.2.3.4, 203.0.113.9" }))).toBe("203.0.113.9");
    expect(clientIpFrom(new Headers({ "x-forwarded-for": "2001:db8::1" }))).toBe("2001:db8::1");
    expect(clientIpFrom(new Headers({ "x-real-ip": "999.1.1.1" }))).toBeNull();
    expect(clientIpFrom(new Headers({ "x-forwarded-for": "evil<script>" }))).toBeNull();
    expect(clientIpFrom(new Headers())).toBeNull();
  });
});

describe("apply wire format", () => {
  const valid = {
    consent: { notice_version: "2026-10-1", accepted: true },
    contact: { full_name: "Layla Haddad", email: "layla@example.test", phone: "", locale: "ar" },
    answers: { notice: "1 month" },
    knockouts: { right_to_work_uae: "yes" },
    self_identification: { gender: "woman" },
    turnstile_token: "XXXX.DUMMY.TOKEN.XXXX",
  };

  it("accepts a well formed body", () => {
    const body = parseApplyBody(valid);
    expect(body?.contact).toEqual({
      full_name: "Layla Haddad",
      email: "layla@example.test",
      phone: null,
      location: null,
      profile_url: null,
      locale: "ar",
    });
  });

  it.each([
    ["no consent block", { ...valid, consent: undefined }],
    ["a knockout that is not yes or no", { ...valid, knockouts: { a: "maybe" } }],
    ["an answer key that is not an id", { ...valid, answers: { "Not An Id": "x" } }],
    ["a long name", { ...valid, contact: { ...valid.contact, full_name: "x".repeat(201) } }],
    ["a non string answer", { ...valid, answers: { notice: 3 } }],
  ])("refuses %s", (_label, body) => {
    expect(parseApplyBody(body)).toBeNull();
  });

  it("only takes same-origin requests with the apply header", () => {
    const origins = ["http://localhost:3100"];
    const ok = new Headers({ "sec-fetch-site": "same-origin", origin: origins[0]!, "x-pairly-apply": "1" });
    expect(checkApplyRequest(ok, origins)).toBeNull();
    expect(checkApplyRequest(new Headers({ "sec-fetch-site": "cross-site", "x-pairly-apply": "1" }), origins)?.code).toBe(
      "cross_site",
    );
    expect(checkApplyRequest(new Headers({ "sec-fetch-site": "same-origin" }), origins)?.code).toBe("cross_site");
    expect(checkApplyRequest(new Headers({ origin: "https://evil.example", "x-pairly-apply": "1" }), origins)?.code).toBe(
      "cross_site",
    );
  });

  it("checks tokens, slugs and error codes", () => {
    expect(isUploadToken("a".repeat(43))).toBe(true);
    expect(isUploadToken("short")).toBe(false);
    expect(isUploadToken("a/b".repeat(10))).toBe(false);
    expect(isSlug("acme-logistics")).toBe(true);
    expect(isSlug("../etc")).toBe(false);
    expect(knownCode("disposable_email")).toBe("disposable_email");
    expect(knownCode("something_new")).toBe("unavailable");
  });

  it("uses no em dash in its messages", async () => {
    const en = (await import("../../../messages/en.json")).default;
    const ar = (await import("../../../messages/ar.json")).default;
    expect(JSON.stringify(en.Careers)).not.toContain(EM_DASH);
    expect(JSON.stringify(ar.Careers)).not.toContain(EM_DASH);
    expect(Object.keys(ar.Careers).sort()).toEqual(Object.keys(en.Careers).sort());
    expect(Object.keys(ar.Careers.errors).sort()).toEqual(Object.keys(en.Careers.errors).sort());
  });
});
