// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Pure helpers for the job editor: applying an inclusive-language suggestion, the rubric
 * shape (`pairly_hire.rubric.Rubric`, sent to the API as a plain object) and the checks
 * the browser runs before saving one. The API validates everything again.
 */
import type { HireFinding, HireJob, HireJobInput } from "./types";

export const SCALE_LEVELS = 5; // scores 0 to 4
export const MAX_CRITERIA = 12;
export const MAX_KNOCKOUTS = 6;

export interface RubricCriterion {
  id: string;
  name: string;
  description: string;
  evidence: string;
  weight: number;
  anchors: string[];
}

export interface RubricKnockout {
  id: string;
  question: string;
  question_ar: string;
  expected_answer: "yes" | "no";
  reason: string;
}

export interface RubricDraft {
  schema_version: 1;
  job_title: string;
  must_haves: RubricCriterion[];
  nice_to_haves: RubricCriterion[];
  knockout_questions: RubricKnockout[];
}

export type WorkModel = HireJobInput["work_model"];
export type EmploymentType = HireJobInput["employment_type"];
export type SalaryPeriod = "year" | "month" | "hour";

export const WORK_MODELS: WorkModel[] = ["onsite", "hybrid", "remote"];
export const EMPLOYMENT_TYPES: EmploymentType[] = ["full_time", "part_time", "contract", "temporary", "internship"];
export const SALARY_PERIODS: SalaryPeriod[] = ["month", "year", "hour"];

/** The editable job fields, as the form holds them (salaries as typed text). */
export interface JobFields {
  title: string;
  title_ar: string;
  team: string;
  location: string;
  country: string;
  work_model: WorkModel;
  employment_type: EmploymentType;
  salary_min: string;
  salary_max: string;
  salary_currency: string;
  salary_period: SalaryPeriod;
  show_salary: boolean;
  description_en: string;
  description_ar: string;
}

export const EMPTY_FIELDS: JobFields = {
  title: "",
  title_ar: "",
  team: "",
  location: "",
  country: "AE",
  work_model: "onsite",
  employment_type: "full_time",
  salary_min: "",
  salary_max: "",
  salary_currency: "AED",
  salary_period: "month",
  show_salary: false,
  description_en: "",
  description_ar: "",
};

export function fieldsOf(job: HireJob): JobFields {
  const period = SALARY_PERIODS.includes(job.salary_period as SalaryPeriod) ? (job.salary_period as SalaryPeriod) : "month";
  return {
    title: job.title,
    title_ar: job.title_ar,
    team: job.team ?? "",
    location: job.location,
    country: job.country ?? "",
    work_model: (WORK_MODELS.includes(job.work_model as WorkModel) ? job.work_model : "onsite") as WorkModel,
    employment_type: (EMPLOYMENT_TYPES.includes(job.employment_type as EmploymentType)
      ? job.employment_type
      : "full_time") as EmploymentType,
    salary_min: job.salary_min ? String(Number(job.salary_min)) : "",
    salary_max: job.salary_max ? String(Number(job.salary_max)) : "",
    salary_currency: job.salary_currency ?? "AED",
    salary_period: period,
    show_salary: job.show_salary,
    description_en: job.description_en,
    description_ar: job.description_ar,
  };
}

const MONEY_RE = /^\d{1,12}(\.\d{1,2})?$/;

export type FieldsProblem = "title" | "location" | "salary" | null;

/** The first thing that keeps the fields from saving, or null. */
export function fieldsProblem(fields: JobFields): FieldsProblem {
  if (fields.title.trim().length < 2) return "title";
  if (fields.location.trim().length < 2) return "location";
  const min = fields.salary_min.trim();
  const max = fields.salary_max.trim();
  if ((min && !MONEY_RE.test(min)) || (max && !MONEY_RE.test(max))) return "salary";
  if (min && max && Number(min) > Number(max)) return "salary";
  return null;
}

/** The API body for the fields: empty optional text becomes null, salaries strings. */
export function jobBody(fields: JobFields): HireJobInput {
  const text = (v: string) => (v.trim() ? v.trim() : null);
  const hasSalary = Boolean(fields.salary_min.trim() || fields.salary_max.trim());
  return {
    title: fields.title.trim(),
    title_ar: fields.title_ar.trim(),
    team: text(fields.team),
    location: fields.location.trim(),
    country: /^[A-Z]{2}$/.test(fields.country.trim().toUpperCase()) ? fields.country.trim().toUpperCase() : null,
    work_model: fields.work_model,
    employment_type: fields.employment_type,
    salary_min: text(fields.salary_min),
    salary_max: text(fields.salary_max),
    salary_currency: hasSalary && /^[A-Z]{3}$/.test(fields.salary_currency.trim().toUpperCase())
      ? fields.salary_currency.trim().toUpperCase()
      : null,
    salary_period: hasSalary ? fields.salary_period : null,
    show_salary: fields.show_salary,
    description_en: fields.description_en,
    description_ar: fields.description_ar,
  };
}

/**
 * Apply an inclusive-language suggestion. The API checks `title + "\n" + description`,
 * so a finding's offsets point into that joined text: a finding before the line break is
 * in the title, after it in the description. If the text changed since the check (the
 * offsets no longer cover the match), the first occurrence of the match is replaced.
 * Returns null when the match is no longer there.
 */
export function applySuggestion(
  title: string,
  description: string,
  finding: Pick<HireFinding, "start" | "end" | "match">,
  replacement: string,
): { title: string; description: string } | null {
  const joined = `${title}\n${description}`;
  let start = finding.start;
  let end = finding.end;
  if (joined.slice(start, end) !== finding.match) {
    start = joined.indexOf(finding.match);
    if (start < 0 || !finding.match) return null;
    end = start + finding.match.length;
  }
  const next = joined.slice(0, start) + replacement + joined.slice(end);
  if (start < title.length && end <= title.length) {
    return { title: next.slice(0, next.length - description.length - 1), description };
  }
  if (start > title.length) {
    return { title, description: next.slice(title.length + 1) };
  }
  return null; // spans the line break: never from a real check
}

// ------------------------------------------------------------------ rubric

const ID_RE = /^[a-z][a-z0-9_]{0,47}$/;

/** An id for a new criterion or question from its name, unique among `taken`. */
export function idFor(name: string, taken: Iterable<string>, fallback = "criterion"): string {
  const used = new Set(taken);
  let base = name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  if (!/^[a-z]/.test(base)) base = base ? `${fallback}_${base}`.slice(0, 40) : fallback;
  let id = base;
  for (let n = 2; used.has(id); n += 1) id = `${base}_${n}`;
  return id;
}

export function newCriterion(taken: Iterable<string>): RubricCriterion {
  return { id: idFor("criterion", taken), name: "", description: "", evidence: "", weight: 5, anchors: [] };
}

export function newKnockout(taken: Iterable<string>): RubricKnockout {
  return { id: idFor("question", taken, "question"), question: "", question_ar: "", expected_answer: "yes", reason: "" };
}

/** A rubric from the API's plain object (missing optional parts filled in). */
export function rubricFrom(value: Record<string, unknown> | null | undefined, title: string): RubricDraft | null {
  if (!value) return null;
  const v = value as Partial<RubricDraft>;
  const criterion = (c: Partial<RubricCriterion>): RubricCriterion => ({
    id: c.id ?? "",
    name: c.name ?? "",
    description: c.description ?? "",
    evidence: c.evidence ?? "",
    weight: typeof c.weight === "number" ? c.weight : 5,
    anchors: Array.isArray(c.anchors) ? [...c.anchors] : [],
  });
  return {
    schema_version: 1,
    job_title: v.job_title || title,
    must_haves: (v.must_haves ?? []).map(criterion),
    nice_to_haves: (v.nice_to_haves ?? []).map(criterion),
    knockout_questions: (v.knockout_questions ?? []).map((k) => ({
      id: k.id ?? "",
      question: k.question ?? "",
      question_ar: k.question_ar ?? "",
      expected_answer: k.expected_answer === "no" ? "no" : "yes",
      reason: k.reason ?? "",
    })),
  };
}

export type RubricProblem =
  | { kind: "no_must_haves" }
  | { kind: "too_many" }
  | { kind: "criterion"; group: "must_haves" | "nice_to_haves"; index: number; field: "name" | "description" | "evidence" | "weight" | "anchors" | "id" }
  | { kind: "knockout"; index: number; field: "question" | "reason" | "id" };

/** The first thing the API would refuse in this rubric, or null. */
export function rubricProblem(rubric: RubricDraft): RubricProblem | null {
  if (rubric.must_haves.length === 0) return { kind: "no_must_haves" };
  if (
    rubric.must_haves.length > MAX_CRITERIA ||
    rubric.nice_to_haves.length > MAX_CRITERIA ||
    rubric.knockout_questions.length > MAX_KNOCKOUTS
  ) {
    return { kind: "too_many" };
  }
  const seen = new Set<string>();
  for (const group of ["must_haves", "nice_to_haves"] as const) {
    for (const [index, c] of rubric[group].entries()) {
      if (!ID_RE.test(c.id) || seen.has(c.id)) return { kind: "criterion", group, index, field: "id" };
      seen.add(c.id);
      if (!c.name.trim() || c.name.trim().length > 160) return { kind: "criterion", group, index, field: "name" };
      if (!c.description.trim() || c.description.length > 1200) return { kind: "criterion", group, index, field: "description" };
      if (!c.evidence.trim() || c.evidence.length > 1200) return { kind: "criterion", group, index, field: "evidence" };
      if (!Number.isInteger(c.weight) || c.weight < 1 || c.weight > 10) return { kind: "criterion", group, index, field: "weight" };
      const anchors = c.anchors.map((a) => a.trim());
      if (anchors.some(Boolean) && (anchors.length !== SCALE_LEVELS || anchors.some((a) => a.length < 3 || a.length > 300))) {
        return { kind: "criterion", group, index, field: "anchors" };
      }
    }
  }
  const questions = new Set<string>();
  for (const [index, k] of rubric.knockout_questions.entries()) {
    if (!ID_RE.test(k.id) || questions.has(k.id)) return { kind: "knockout", index, field: "id" };
    questions.add(k.id);
    if (!k.question.trim() || k.question.length > 1200) return { kind: "knockout", index, field: "question" };
    if (!k.reason.trim() || k.reason.length > 1200) return { kind: "knockout", index, field: "reason" };
  }
  return null;
}

/** The body the API takes: anchors left out when none are written. */
export function rubricBody(rubric: RubricDraft): Record<string, unknown> {
  const criterion = (c: RubricCriterion) => {
    const anchors = c.anchors.map((a) => a.trim());
    return {
      id: c.id,
      name: c.name.trim(),
      description: c.description.trim(),
      evidence: c.evidence.trim(),
      weight: c.weight,
      ...(anchors.some(Boolean) ? { anchors } : {}),
    };
  };
  return {
    schema_version: 1,
    job_title: rubric.job_title.trim().slice(0, 160),
    must_haves: rubric.must_haves.map(criterion),
    nice_to_haves: rubric.nice_to_haves.map(criterion),
    knockout_questions: rubric.knockout_questions.map((k) => ({
      id: k.id,
      question: k.question.trim(),
      question_ar: k.question_ar.trim(),
      expected_answer: k.expected_answer,
      reason: k.reason.trim(),
    })),
  };
}
