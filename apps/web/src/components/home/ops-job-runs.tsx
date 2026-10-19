// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { Activity } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
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
  if (runs.length === 0) return <EmptyState icon={<Activity aria-hidden />} title={t("empty")} />;
  return (
    <ul className="card-hairline divide-y divide-border" data-ops-runs={runs.length}>
      {runs.map((run) => (
        <li key={run.id} className="space-y-1 px-4 py-3 text-sm" data-ops-run={run.status}>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <Link href={`/ops/jobs?job=${run.job_name}`} className="font-mono font-medium text-primary underline-offset-4 hover:underline" dir="ltr">
              {run.job_name}
            </Link>
            <Badge variant={run.status === "failed" ? "danger" : run.status === "succeeded" ? "success" : "outline"}>
              {status.has(run.status as "ok") ? status(run.status as "ok") : run.status}
            </Badge>
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
            <p className="text-xs text-danger-ink">
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
