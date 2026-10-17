// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";
import type { OpsJobRun } from "@/lib/system-dashboards";

function summaryText(summary: OpsJobRun["summary"]): string {
  if (!summary) return "";
  return Object.entries(summary)
    .filter(([, v]) => v === null || ["number", "boolean", "string"].includes(typeof v))
    .slice(0, 8)
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join(" · ");
}

/** Recent job runs as a list that reads on a phone (one card per run). */
export function OpsJobRuns({ runs }: { runs: OpsJobRun[] }) {
  const t = useTranslations("OpsJobs");
  const status = useTranslations("SystemDashboards.status");
  const format = useFormatter();
  if (runs.length === 0) return <p className="text-sm text-muted-foreground">{t("empty")}</p>;
  return (
    <ul className="divide-y rounded-xl border bg-surface" data-ops-runs={runs.length}>
      {runs.map((run) => (
        <li key={run.id} className="space-y-1 p-3 text-sm" data-ops-run={run.status}>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <Link href={`/ops/jobs?job=${run.job_name}`} className="font-mono font-medium hover:text-primary hover:underline" dir="ltr">
              {run.job_name}
            </Link>
            <span className={`rounded border px-1.5 text-xs ${run.status === "failed" ? "text-destructive" : ""}`}>
              {status.has(run.status as "ok") ? status(run.status as "ok") : run.status}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("started")}: <time dateTime={run.started_at}>{format.dateTime(new Date(run.started_at), { dateStyle: "medium", timeStyle: "medium" })}</time>
            {" · "}
            {t("finished")}:{" "}
            {run.finished_at ? (
              <time dateTime={run.finished_at}>{format.dateTime(new Date(run.finished_at), { dateStyle: "medium", timeStyle: "medium" })}</time>
            ) : (
              t("running")
            )}
          </p>
          {run.error && (
            <p className="text-xs text-destructive">
              {t("error")}: <code dir="ltr">{run.error}</code>
            </p>
          )}
          {summaryText(run.summary) && (
            <p className="text-xs text-muted-foreground" dir="ltr">
              {t("summary")}: {summaryText(run.summary)}
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}
