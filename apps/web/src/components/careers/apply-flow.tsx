// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

/**
 * The public apply flow (brief 3.2), three steps on one page:
 *
 * 1. How we use AI: the notice in plain words and the consent checkbox. Nothing is sent
 *    before the candidate confirms; the notice version goes with the application.
 * 2. The application: contact details, the job's questions, the knockout questions
 *    (a different answer goes to a person, never a rejection), the CV and optional
 *    files, the optional equal opportunity questions in their own clearly separate
 *    section, and the human check.
 * 3. Sent: the reference and when to expect an answer.
 *
 * Sending is three calls to this origin's route handlers: start (JSON), one upload per
 * file, submit. Accessibility (WCAG 2.2 AA): every field has a visible label, errors
 * are tied to their field (`aria-describedby`, `aria-invalid`) and announced, focus
 * moves to the heading of each step and to the first field with an error, targets are
 * at least 44 px high, and everything works with the keyboard alone.
 */
import { CheckCircle2, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { Checkbox, Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Link } from "@/i18n/navigation";
import {
  ACCEPTED_FILES,
  APPLY_HEADER,
  applyPath,
  filesPath,
  isReferralToken,
  knownCode,
  MAX_CV_BYTES,
  submitPath,
  type ApplyBody,
  type KnownCode,
} from "@/lib/careers/apply";
import { pick, type Locale, type PublicForm, type PublicNotice } from "@/lib/careers/format";
import { UPLOAD_HEADER } from "@/lib/upload-guard";

import { HumanCheck } from "./human-check";

type Step = "notice" | "form" | "done";
type Errors = Record<string, string>;

export interface ApplyFlowProps {
  orgSlug: string;
  orgName: string;
  jobSlug: string;
  locale: Locale;
  notice: PublicNotice;
  form: PublicForm;
}

/** An employee's referral token in the address (`?ref=`), if the visitor came by one. */
function referralFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  const value = new URLSearchParams(window.location.search).get("ref");
  return isReferralToken(value) ? value : null;
}

class ApplyError extends Error {
  constructor(public code: KnownCode) {
    super(code);
  }
}

async function post(path: string, body: BodyInit, headers: Record<string, string>): Promise<Record<string, unknown>> {
  let response: Response;
  try {
    response = await fetch(path, { method: "POST", body, headers, credentials: "same-origin" });
  } catch {
    throw new ApplyError("unavailable");
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await response.json()) as Record<string, unknown>;
  } catch {
    // An empty or broken body: the status decides.
  }
  if (!response.ok) throw new ApplyError(knownCode(data.code ?? (response.status === 429 ? "rate_limited" : null)));
  return data;
}

function FieldError({ id, message }: { id: string; message?: string | undefined }) {
  if (!message) return null;
  return (
    <p id={id} className="text-sm text-destructive">
      {message}
    </p>
  );
}

export function ApplyFlow({ orgSlug, orgName, jobSlug, locale, notice, form }: ApplyFlowProps) {
  const t = useTranslations("Careers");
  const uid = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const [step, setStep] = useState<Step>("notice");
  const [consent, setConsent] = useState(false);
  const [consentError, setConsentError] = useState<string | null>(null);
  const [contact, setContact] = useState({ full_name: "", email: "", phone: "", location: "", profile_url: "" });
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [knockouts, setKnockouts] = useState<Record<string, "yes" | "no">>({});
  const [selfId, setSelfId] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<Record<string, File | null>>({});
  const [turnstile, setTurnstile] = useState<string | null>(null);
  const [errors, setErrors] = useState<Errors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ reference: string; days: number } | null>(null);

  // Move focus to the new step's heading, so keyboard and screen reader users land there.
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    headingRef.current?.focus();
  }, [step]);

  const steps: Array<{ key: Step; label: string }> = [
    { key: "notice", label: t("noticeStep") },
    { key: "form", label: t("formStep") },
    { key: "done", label: t("doneStep") },
  ];
  const stepIndex = steps.findIndex((s) => s.key === step);
  const fid = (name: string) => `${uid}-${name}`;
  const errorId = (name: string) => `${uid}-${name}-error`;
  const describe = (name: string, hint?: boolean) =>
    [hint ? `${fid(name)}-hint` : null, errors[name] ? errorId(name) : null].filter(Boolean).join(" ") || undefined;

  function validate(): Errors {
    const found: Errors = {};
    const required = (key: string, value: string, label: string) => {
      if (!value.trim()) found[key] = t("fieldRequired", { field: label });
    };
    required("full_name", contact.full_name, t("fullName"));
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contact.email.trim())) found.email = t("errors.invalid_email");
    if (form.standard.phone === "required") required("phone", contact.phone, t("phone"));
    if (form.standard.location === "required") required("location", contact.location, t("locationField"));
    if (form.standard.profile_url === "required") required("profile_url", contact.profile_url, t("profileUrl"));
    if (contact.profile_url.trim() && !contact.profile_url.trim().startsWith("https://")) {
      found.profile_url = t("profileUrlHint");
    }
    for (const q of form.questions) {
      if (q.required && !(answers[q.id] ?? "").trim()) found[`q_${q.id}`] = t("fieldRequired", { field: pick(locale, q.label, q.label_ar) });
    }
    for (const k of form.knockout_questions) {
      if (!knockouts[k.id]) found[`k_${k.id}`] = t("fieldRequired", { field: pick(locale, k.question, k.question_ar) });
    }
    for (const f of form.files) {
      const file = files[f.id];
      const label = f.id === "cv" ? t("cv") : pick(locale, f.label, f.label_ar);
      if (f.required && !file) found[`f_${f.id}`] = t("fieldRequired", { field: label });
      if (file && file.size > MAX_CV_BYTES) found[`f_${f.id}`] = t("fileTooLarge", { name: file.name });
    }
    if (!turnstile) found.turnstile = t("humanCheckRequired");
    return found;
  }

  function focusFirstError(found: Errors) {
    const first = Object.keys(found)[0];
    if (!first) return;
    const target = formRef.current?.querySelector<HTMLElement>(`[data-field="${first}"]`);
    target?.focus();
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    setFormError(null);
    const found = validate();
    setErrors(found);
    if (Object.keys(found).length > 0) {
      focusFirstError(found);
      return;
    }
    setBusy(true);
    setStatus(t("sending"));
    const body: ApplyBody = {
      consent: { notice_version: notice.version, accepted: consent },
      contact: {
        full_name: contact.full_name.trim(),
        email: contact.email.trim(),
        phone: contact.phone.trim() || null,
        location: contact.location.trim() || null,
        profile_url: contact.profile_url.trim() || null,
        locale,
      },
      answers: Object.fromEntries(Object.entries(answers).filter(([, v]) => v.trim())),
      knockouts,
      self_identification: Object.fromEntries(Object.entries(selfId).filter(([, v]) => v)),
      turnstile_token: turnstile ?? "",
      ...(referralFromUrl() ? { referral: referralFromUrl() ?? "" } : {}),
    };
    try {
      const started = await post(applyPath(orgSlug, jobSlug), JSON.stringify(body), {
        "Content-Type": "application/json",
        [APPLY_HEADER]: "1",
      });
      const token = typeof started.token === "string" ? started.token : "";
      for (const f of form.files) {
        const file = files[f.id];
        if (!file) continue;
        setStatus(t("uploading", { name: file.name }));
        const upload = new FormData();
        upload.set("token", token);
        upload.set("field", f.id);
        upload.set("file", file, file.name);
        await post(filesPath(orgSlug), upload, { [UPLOAD_HEADER]: "1" });
      }
      const submitted = await post(submitPath(orgSlug), JSON.stringify({ token }), {
        "Content-Type": "application/json",
        [APPLY_HEADER]: "1",
      });
      setDone({
        reference: String(submitted.reference ?? ""),
        days: typeof submitted.response_days === "number" ? submitted.response_days : 10,
      });
      setStatus("");
      setStep("done");
    } catch (err) {
      const code = err instanceof ApplyError ? err.code : "unavailable";
      if (code === "bot_check_failed") setTurnstile(null);
      setFormError(t(`errors.${code}`));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-8">
      <nav aria-label={t("stepLabel", { step: stepIndex + 1, total: steps.length })}>
        <ol className="flex flex-wrap gap-2 text-sm">
          {steps.map((s, i) => (
            <li
              key={s.key}
              aria-current={s.key === step ? "step" : undefined}
              className={
                s.key === step
                  ? "rounded-full bg-primary px-3 py-1 text-primary-foreground"
                  : i < stepIndex
                    ? "rounded-full bg-secondary px-3 py-1 text-secondary-foreground"
                    : "rounded-full border border-border px-3 py-1 text-muted-foreground"
              }
            >
              <span className="sr-only">{t("stepLabel", { step: i + 1, total: steps.length })}: </span>
              {s.label}
            </li>
          ))}
        </ol>
      </nav>

      <p role="status" aria-live="polite" className="sr-only">
        {status}
      </p>

      {step === "notice" && (
        <section aria-labelledby={fid("notice-heading")} className="space-y-6">
          <h2 id={fid("notice-heading")} ref={headingRef} tabIndex={-1} className="font-display text-2xl outline-none">
            {notice.title}
          </h2>
          <ul className="space-y-3">
            {notice.points.map((point, i) => (
              <li key={i} className="flex gap-3 leading-relaxed">
                <ShieldCheck aria-hidden className="mt-1 size-4 shrink-0 text-signal" />
                <span>{point}</span>
              </li>
            ))}
          </ul>
          <div className="space-y-2">
            <label htmlFor={fid("consent")} className="flex min-h-11 items-start gap-3 rounded-lg border border-input bg-surface p-3">
              <Checkbox
                id={fid("consent")}
                className="mt-0.5"
                checked={consent}
                aria-invalid={consentError ? true : undefined}
                aria-describedby={consentError ? errorId("consent") : undefined}
                onChange={(e) => {
                  setConsent(e.target.checked);
                  if (e.target.checked) setConsentError(null);
                }}
              />
              <span>{notice.consent}</span>
            </label>
            <FieldError id={errorId("consent")} message={consentError ?? undefined} />
          </div>
          <Button
            size="lg"
            onClick={() => {
              if (!consent) {
                setConsentError(t("consentRequired"));
                return;
              }
              setStep("form");
            }}
          >
            {t("continue")}
          </Button>
        </section>
      )}

      {step === "form" && (
        <form ref={formRef} noValidate onSubmit={send} className="space-y-8" aria-labelledby={fid("form-heading")}>
          <h2 id={fid("form-heading")} ref={headingRef} tabIndex={-1} className="font-display text-2xl outline-none">
            {t("formStep")}
          </h2>
          {formError && (
            <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
              {formError}
            </div>
          )}

          <fieldset className="space-y-4">
            <legend className="mb-2 text-lg font-medium">{t("yourDetails")}</legend>
            <TextField
              id={fid("full_name")}
              field="full_name"
              label={t("fullName")}
              autoComplete="name"
              value={contact.full_name}
              required
              error={errors.full_name}
              errorId={errorId("full_name")}
              onChange={(v) => setContact({ ...contact, full_name: v })}
            />
            <TextField
              id={fid("email")}
              field="email"
              type="email"
              label={t("email")}
              autoComplete="email"
              hint={t("emailHint")}
              value={contact.email}
              required
              error={errors.email}
              errorId={errorId("email")}
              onChange={(v) => setContact({ ...contact, email: v })}
            />
            {form.standard.phone !== "hidden" && (
              <TextField
                id={fid("phone")}
                field="phone"
                type="tel"
                label={t("phone")}
                autoComplete="tel"
                value={contact.phone}
                required={form.standard.phone === "required"}
                error={errors.phone}
                errorId={errorId("phone")}
                onChange={(v) => setContact({ ...contact, phone: v })}
              />
            )}
            {form.standard.location !== "hidden" && (
              <TextField
                id={fid("location")}
                field="location"
                label={t("locationField")}
                autoComplete="address-level2"
                value={contact.location}
                required={form.standard.location === "required"}
                error={errors.location}
                errorId={errorId("location")}
                onChange={(v) => setContact({ ...contact, location: v })}
              />
            )}
            {form.standard.profile_url !== "hidden" && (
              <TextField
                id={fid("profile_url")}
                field="profile_url"
                type="url"
                label={t("profileUrl")}
                autoComplete="url"
                hint={t("profileUrlHint")}
                value={contact.profile_url}
                required={form.standard.profile_url === "required"}
                error={errors.profile_url}
                errorId={errorId("profile_url")}
                onChange={(v) => setContact({ ...contact, profile_url: v })}
              />
            )}
          </fieldset>

          {form.questions.length > 0 && (
            <fieldset className="space-y-5">
              <legend className="mb-2 text-lg font-medium">{t("questions")}</legend>
              {form.questions.map((q) => {
                const key = `q_${q.id}`;
                const label = pick(locale, q.label, q.label_ar);
                if (q.kind === "text") {
                  return (
                    <div key={q.id} className="space-y-2">
                      <FieldLabel htmlFor={fid(key)} label={label} required={q.required} />
                      <Textarea
                        id={fid(key)}
                        data-field={key}
                        maxLength={q.max_length}
                        value={answers[q.id] ?? ""}
                        aria-invalid={errors[key] ? true : undefined}
                        aria-describedby={describe(key)}
                        onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                      />
                      <FieldError id={errorId(key)} message={errors[key]} />
                    </div>
                  );
                }
                const choices =
                  q.kind === "yes_no"
                    ? [
                        { id: "yes", label: t("yes") },
                        { id: "no", label: t("no") },
                      ]
                    : q.options.map((o) => ({ id: o.id, label: pick(locale, o.label, o.label_ar) }));
                return (
                  <RadioGroup
                    key={q.id}
                    name={fid(key)}
                    field={key}
                    legend={label}
                    required={q.required}
                    choices={choices}
                    value={answers[q.id] ?? ""}
                    error={errors[key]}
                    errorId={errorId(key)}
                    onChange={(v) => setAnswers({ ...answers, [q.id]: v })}
                  />
                );
              })}
            </fieldset>
          )}

          {form.knockout_questions.length > 0 && (
            <fieldset className="space-y-5" aria-describedby={`${fid("knockouts")}-hint`}>
              <legend className="text-lg font-medium">{t("knockouts")}</legend>
              <p id={`${fid("knockouts")}-hint`} className="text-sm text-muted-foreground">
                {t("knockoutsHint")}
              </p>
              {form.knockout_questions.map((k) => {
                const key = `k_${k.id}`;
                return (
                  <RadioGroup
                    key={k.id}
                    name={fid(key)}
                    field={key}
                    legend={pick(locale, k.question, k.question_ar)}
                    required
                    choices={[
                      { id: "yes", label: t("yes") },
                      { id: "no", label: t("no") },
                    ]}
                    value={knockouts[k.id] ?? ""}
                    error={errors[key]}
                    errorId={errorId(key)}
                    onChange={(v) => setKnockouts({ ...knockouts, [k.id]: v === "yes" ? "yes" : "no" })}
                  />
                );
              })}
            </fieldset>
          )}

          <fieldset className="space-y-4">
            <legend className="mb-2 text-lg font-medium">{t("files")}</legend>
            {form.files.map((f) => {
              const key = `f_${f.id}`;
              const label = f.id === "cv" ? t("cv") : pick(locale, f.label, f.label_ar);
              return (
                <div key={f.id} className="space-y-2">
                  <FieldLabel htmlFor={fid(key)} label={label} required={f.required} />
                  <input
                    id={fid(key)}
                    data-field={key}
                    type="file"
                    accept={ACCEPTED_FILES}
                    aria-invalid={errors[key] ? true : undefined}
                    aria-describedby={[`${fid(key)}-hint`, errors[key] ? errorId(key) : null].filter(Boolean).join(" ")}
                    className="block min-h-11 w-full rounded-md border border-input bg-surface p-2 text-sm file:me-3 file:rounded file:border-0 file:bg-secondary file:px-3 file:py-1.5 file:text-secondary-foreground focus-visible:outline-2 focus-visible:outline-ring"
                    onChange={(e) => setFiles({ ...files, [f.id]: e.target.files?.[0] ?? null })}
                  />
                  <p id={`${fid(key)}-hint`} className="text-xs text-muted-foreground">
                    {t("fileHint")}
                  </p>
                  <FieldError id={errorId(key)} message={errors[key]} />
                </div>
              );
            })}
          </fieldset>

          {form.self_identification.length > 0 && (
            <fieldset
              className="space-y-4 rounded-xl border border-dashed border-border bg-muted/30 p-4 sm:p-5"
              aria-describedby={`${fid("selfid")}-hint`}
            >
              <legend className="px-1 text-lg font-medium">{t("selfId")}</legend>
              <p id={`${fid("selfid")}-hint`} className="text-sm leading-relaxed text-muted-foreground">
                {t("selfIdHint")}
              </p>
              {form.self_identification.map((c) => (
                <div key={c.id} className="space-y-2">
                  <FieldLabel htmlFor={fid(`s_${c.id}`)} label={pick(locale, c.label, c.label_ar)} required={false} />
                  <Select
                    id={fid(`s_${c.id}`)}
                    value={selfId[c.id] ?? ""}
                    onChange={(e) => setSelfId({ ...selfId, [c.id]: e.target.value })}
                  >
                    <option value="">{t("skip")}</option>
                    {c.options.map((o) => (
                      <option key={o.id} value={o.id}>
                        {pick(locale, o.label, o.label_ar)}
                      </option>
                    ))}
                  </Select>
                </div>
              ))}
            </fieldset>
          )}

          <div data-field="turnstile" tabIndex={-1} className="outline-none">
            <HumanCheck token={turnstile} onToken={setTurnstile} error={errors.turnstile} />
          </div>

          <Button type="submit" size="lg" disabled={busy} aria-busy={busy}>
            {busy ? t("sending") : t("submit")}
          </Button>
        </form>
      )}

      {step === "done" && done && (
        <section aria-labelledby={fid("done-heading")} className="space-y-4">
          <CheckCircle2 aria-hidden className="size-10 text-primary" />
          <h2 id={fid("done-heading")} ref={headingRef} tabIndex={-1} className="font-display text-3xl outline-none">
            {t("doneHeading")}
          </h2>
          <p className="text-lg">{t("doneReference", { reference: done.reference })}</p>
          <p>{t("doneTimeline", { days: done.days })}</p>
          <p className="text-muted-foreground">{t("doneEmail")}</p>
          <p>
            <Link href={`/careers/${orgSlug}`} className="font-medium text-primary underline-offset-4 hover:underline">
              {t("allRoles", { org: orgName })}
            </Link>
          </p>
        </section>
      )}
    </div>
  );
}

function FieldLabel({ htmlFor, label, required }: { htmlFor: string; label: string; required: boolean }) {
  const t = useTranslations("Careers");
  return (
    <label htmlFor={htmlFor} className="block text-sm font-medium">
      {label}{" "}
      <span className="ms-1 text-xs font-normal text-muted-foreground">({required ? t("required") : t("optional")})</span>
    </label>
  );
}

function TextField({
  id,
  field,
  label,
  value,
  onChange,
  required = false,
  type = "text",
  hint,
  error,
  errorId,
  autoComplete,
}: {
  id: string;
  field: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  type?: string | undefined;
  hint?: string | undefined;
  error?: string | undefined;
  errorId: string;
  autoComplete?: string | undefined;
}) {
  const described = [hint ? `${id}-hint` : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="space-y-2">
      <FieldLabel htmlFor={id} label={label} required={required} />
      <Input
        id={id}
        data-field={field}
        type={type}
        value={value}
        autoComplete={autoComplete}
        aria-required={required || undefined}
        aria-invalid={error ? true : undefined}
        aria-describedby={described}
        className="h-11"
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      <FieldError id={errorId} message={error} />
    </div>
  );
}

function RadioGroup({
  name,
  field,
  legend,
  required,
  choices,
  value,
  error,
  errorId,
  onChange,
}: {
  name: string;
  field: string;
  legend: string;
  required: boolean;
  choices: Array<{ id: string; label: string }>;
  value: string;
  error?: string | undefined;
  errorId: string;
  onChange: (value: string) => void;
}) {
  const t = useTranslations("Careers");
  return (
    <fieldset className="space-y-2" aria-describedby={error ? errorId : undefined} aria-invalid={error ? true : undefined}>
      <legend className="text-sm font-medium">
        {legend}{" "}
        <span className="ms-1 text-xs font-normal text-muted-foreground">({required ? t("required") : t("optional")})</span>
      </legend>
      <div className="flex flex-wrap gap-2">
        {choices.map((choice, index) => (
          <label
            key={choice.id}
            className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-input bg-surface px-3 has-[:checked]:border-primary has-[:checked]:bg-accent has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-ring"
          >
            <input
              type="radio"
              name={name}
              value={choice.id}
              checked={value === choice.id}
              data-field={index === 0 ? field : undefined}
              className="size-4 accent-primary"
              onChange={() => onChange(choice.id)}
            />
            <span className="text-sm">{choice.label}</span>
          </label>
        ))}
      </div>
      <FieldError id={errorId} message={error} />
    </fieldset>
  );
}
