// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Building blocks of the public careers pages: the job list, the facts of one job, its
 * description. No client state, so they render on the server and in unit tests alike.
 * Semantic HTML first (lists, a description list, headings in order) so screen readers
 * and search engines read the same structure people see.
 */
import { ArrowRight, BriefcaseBusiness, MapPin, Users, Wallet } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import type * as React from "react";

import { Link } from "@/i18n/navigation";
import {
  descriptionBlocks,
  localTitle,
  salaryRange,
  type Locale,
  type PublicJob,
  type Salary,
} from "@/lib/careers/format";

function useLocal(): Locale {
  return useLocale() === "ar" ? "ar" : "en";
}

export function SalaryText({ salary }: { salary: Salary | null | undefined }) {
  const t = useTranslations("Careers");
  const range = salaryRange(salary, useLocal());
  if (!range || !salary) return <>{t("salaryHidden")}</>;
  return <>{t("salary", { ...range, period: salary.period ?? "other" })}</>;
}

function Fact({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <dt className="flex items-center gap-1.5 text-muted-foreground">
        <span aria-hidden className="[&_svg]:size-4">
          {icon}
        </span>
        <span className="sr-only">{label}</span>
      </dt>
      <dd>{children}</dd>
    </div>
  );
}

/** Location, work model, job type, team and pay, as a description list. */
export function JobFacts({ job, className }: { job: PublicJob; className?: string }) {
  const t = useTranslations("Careers");
  const workModel = ["onsite", "hybrid", "remote"].includes(job.work_model) ? job.work_model : "onsite";
  const type = ["full_time", "part_time", "contract", "temporary", "internship"].includes(job.employment_type)
    ? job.employment_type
    : "full_time";
  return (
    <dl className={className ?? "flex flex-wrap gap-x-5 gap-y-2 text-sm"}>
      <Fact icon={<MapPin />} label={t("location")}>
        {job.location} · {t(`workModels.${workModel as "onsite"}`)}
      </Fact>
      <Fact icon={<BriefcaseBusiness />} label={t("employmentType")}>
        {t(`employmentTypes.${type as "full_time"}`)}
      </Fact>
      {job.team && (
        <Fact icon={<Users />} label={t("team")}>
          {job.team}
        </Fact>
      )}
      <Fact icon={<Wallet />} label={t("pay")}>
        <SalaryText salary={job.salary} />
      </Fact>
    </dl>
  );
}

/** Column spans for the asymmetric two-column grid: 3/2, then 2/3, and so on. */
function spanFor(index: number, total: number): string {
  if (index === total - 1 && total % 2 === 1) return "md:col-span-5";
  const row = Math.floor(index / 2);
  const first = index % 2 === 0;
  return (row % 2 === 0) === first ? "md:col-span-3" : "md:col-span-2";
}

/**
 * The organisation's open jobs, newest first: hairline cards in an asymmetric
 * two-column grid. The title is the card's link (it covers the card); the saffron
 * "Apply" goes straight to the form.
 */
export function JobList({ orgSlug, jobs }: { orgSlug: string; jobs: PublicJob[] }) {
  const t = useTranslations("Careers");
  const locale = useLocal();
  if (jobs.length === 0) {
    return <p className="rounded-lg border border-dashed border-border p-8 text-muted-foreground">{t("noRoles")}</p>;
  }
  return (
    <ul className="grid gap-4 md:grid-cols-5">
      {jobs.map((job, index) => {
        const title = localTitle(job, locale);
        return (
          <li
            key={job.slug}
            className={`lift group relative flex flex-col justify-between gap-8 rounded-lg border border-border bg-surface p-6 focus-within:border-foreground/40 sm:p-7 ${spanFor(index, jobs.length)}`}
          >
            <div className="space-y-4">
              <h3 className="font-display text-2xl leading-snug text-balance">
                <Link
                  href={`/careers/${orgSlug}/${job.slug}`}
                  className="outline-none after:absolute after:inset-0 after:rounded-lg after:content-[''] focus-visible:underline focus-visible:underline-offset-4"
                >
                  {title}
                </Link>
              </h3>
              <JobFacts job={job} className="flex flex-wrap gap-x-5 gap-y-1.5 text-sm text-muted-foreground" />
            </div>
            <div className="flex items-center justify-between gap-4">
              <span aria-hidden className="inline-flex items-center gap-1.5 text-sm text-muted-foreground group-hover:text-foreground">
                {t("viewRole")}
                <ArrowRight className="size-3.5 transition-transform duration-150 group-hover:translate-x-0.5 rtl:-scale-x-100 rtl:group-hover:-translate-x-0.5" />
              </span>
              <Link
                href={`/careers/${orgSlug}/${job.slug}/apply`}
                aria-label={t("applyFor", { title })}
                className="relative z-10 inline-flex h-9 items-center rounded-md bg-action px-4 text-sm font-medium text-action-foreground hover:bg-action-hover"
              >
                {t("applyShort")}
              </Link>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** Plain job text as sections, paragraphs and lists (the API stores plain text). */
export function JobDescription({ text, headingLevel = 2 }: { text: string; headingLevel?: 2 | 3 }) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  return (
    <div className="space-y-5 text-[1.0625rem] leading-[1.75]">
      {descriptionBlocks(text).map((block, index) => {
        if (block.kind === "heading") {
          return (
            <Heading key={index} className="pt-4 font-display text-2xl leading-snug">
              {block.text}
            </Heading>
          );
        }
        if (block.kind === "list") {
          return (
            <ul key={index} className="list-disc space-y-2 ps-6 marker:text-signal">
              {block.items.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          );
        }
        return <p key={index}>{block.text}</p>;
      })}
    </div>
  );
}
