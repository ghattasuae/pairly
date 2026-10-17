// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Building blocks of the public careers pages: the job list, the facts of one job, its
 * description. No client state, so they render on the server and in unit tests alike.
 * Semantic HTML first (lists, a description list, headings in order) so screen readers
 * and search engines read the same structure people see.
 */
import { BriefcaseBusiness, MapPin, Users, Wallet } from "lucide-react";
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

/** The organisation's open jobs, newest first, each a link to its page. */
export function JobList({ orgSlug, jobs }: { orgSlug: string; jobs: PublicJob[] }) {
  const t = useTranslations("Careers");
  const locale = useLocal();
  if (jobs.length === 0) {
    return <p className="rounded-lg border border-dashed border-border p-6 text-muted-foreground">{t("noRoles")}</p>;
  }
  return (
    <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface shadow-xs">
      {jobs.map((job) => {
        const title = localTitle(job, locale);
        return (
          <li key={job.slug} className="group relative p-5 transition-colors focus-within:bg-accent/60 hover:bg-accent/40 sm:p-6">
            <h3 className="font-display text-xl leading-snug">
              <Link
                href={`/careers/${orgSlug}/${job.slug}`}
                className="outline-none after:absolute after:inset-0 after:content-[''] focus-visible:underline"
              >
                {title}
              </Link>
            </h3>
            <JobFacts job={job} className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-sm text-muted-foreground" />
            <span aria-hidden className="mt-3 inline-block text-sm font-medium text-primary group-hover:underline">
              {t("viewRole")}
            </span>
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
    <div className="space-y-4 text-base leading-relaxed">
      {descriptionBlocks(text).map((block, index) => {
        if (block.kind === "heading") {
          return (
            <Heading key={index} className="pt-2 font-display text-xl">
              {block.text}
            </Heading>
          );
        }
        if (block.kind === "list") {
          return (
            <ul key={index} className="list-disc space-y-1.5 ps-6 marker:text-signal">
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
