// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import {
  applySuggestion,
  EMPTY_FIELDS,
  fieldsProblem,
  idFor,
  jobBody,
  newCriterion,
  rubricBody,
  rubricFrom,
  rubricProblem,
  type RubricDraft,
} from "./job-editor";

const RUBRIC: RubricDraft = {
  schema_version: 1,
  job_title: "Backend Engineer",
  must_haves: [
    { id: "python", name: "Python", description: "Builds services.", evidence: "Shipped services.", weight: 8, anchors: [] },
  ],
  nice_to_haves: [],
  knockout_questions: [
    { id: "work_permit", question: "Can you work in the UAE?", question_ar: "", expected_answer: "yes", reason: "On site role." },
  ],
};

describe("applySuggestion", () => {
  it("replaces a match in the description, offsets counted after the title and a line break", () => {
    const title = "Rockstar engineer";
    const description = "We want a rockstar who works hard.";
    const start = title.length + 1 + description.indexOf("rockstar");
    const out = applySuggestion(title, description, { start, end: start + 8, match: "rockstar" }, "skilled engineer");
    expect(out).toEqual({ title, description: "We want a skilled engineer who works hard." });
  });

  it("replaces a match in the title", () => {
    const out = applySuggestion("Rockstar engineer", "Body", { start: 0, end: 8, match: "Rockstar" }, "Senior");
    expect(out).toEqual({ title: "Senior engineer", description: "Body" });
  });

  it("finds the match again when the text moved, and says when it is gone", () => {
    const out = applySuggestion("T", "Hello, we need a ninja.", { start: 2, end: 7, match: "ninja" }, "expert");
    expect(out?.description).toBe("Hello, we need a expert.");
    expect(applySuggestion("T", "Nothing here", { start: 2, end: 7, match: "ninja" }, "expert")).toBeNull();
  });
});

describe("job fields", () => {
  it("needs a title, a location and a sane salary range", () => {
    expect(fieldsProblem(EMPTY_FIELDS)).toBe("title");
    expect(fieldsProblem({ ...EMPTY_FIELDS, title: "Engineer" })).toBe("location");
    const ok = { ...EMPTY_FIELDS, title: "Engineer", location: "Dubai" };
    expect(fieldsProblem(ok)).toBeNull();
    expect(fieldsProblem({ ...ok, salary_min: "40000", salary_max: "30000" })).toBe("salary");
    expect(fieldsProblem({ ...ok, salary_min: "abc" })).toBe("salary");
  });

  it("sends salaries as strings and leaves currency and period out without a range", () => {
    const base = { ...EMPTY_FIELDS, title: " Engineer ", location: "Dubai" };
    expect(jobBody(base)).toMatchObject({ title: "Engineer", salary_min: null, salary_currency: null, salary_period: null, team: null });
    expect(jobBody({ ...base, salary_min: "30000", salary_max: "38000", show_salary: true })).toMatchObject({
      salary_min: "30000",
      salary_max: "38000",
      salary_currency: "AED",
      salary_period: "month",
      show_salary: true,
    });
  });
});

describe("rubric", () => {
  it("accepts a complete rubric and points at the first missing field", () => {
    expect(rubricProblem(RUBRIC)).toBeNull();
    expect(rubricProblem({ ...RUBRIC, must_haves: [] })).toEqual({ kind: "no_must_haves" });
    const noEvidence = { ...RUBRIC, must_haves: [{ ...RUBRIC.must_haves[0]!, evidence: " " }] };
    expect(rubricProblem(noEvidence)).toMatchObject({ kind: "criterion", group: "must_haves", index: 0, field: "evidence" });
    const heavy = { ...RUBRIC, must_haves: [{ ...RUBRIC.must_haves[0]!, weight: 11 }] };
    expect(rubricProblem(heavy)).toMatchObject({ field: "weight" });
    const partial = { ...RUBRIC, must_haves: [{ ...RUBRIC.must_haves[0]!, anchors: ["None at all", "", "", "", ""] }] };
    expect(rubricProblem(partial)).toMatchObject({ field: "anchors" });
    const noReason = { ...RUBRIC, knockout_questions: [{ ...RUBRIC.knockout_questions[0]!, reason: "" }] };
    expect(rubricProblem(noReason)).toEqual({ kind: "knockout", index: 0, field: "reason" });
  });

  it("round-trips the API object and leaves empty anchors out", () => {
    const parsed = rubricFrom(rubricBody(RUBRIC), "x");
    expect(parsed).toEqual(RUBRIC);
    expect(rubricBody(RUBRIC).must_haves).toEqual([
      { id: "python", name: "Python", description: "Builds services.", evidence: "Shipped services.", weight: 8 },
    ]);
  });

  it("gives new criteria unique valid ids", () => {
    expect(idFor("Python 3!", [])).toBe("python_3");
    expect(idFor("criterion", ["criterion"])).toBe("criterion_2");
    expect(idFor("قيادة", [])).toBe("criterion");
    expect(newCriterion(["criterion"]).id).toBe("criterion_2");
  });
});
