// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox, Select } from "@/components/ui/form-controls";
import { errorKey } from "@/lib/action-result";
import { setRecruiters } from "@/lib/actions/hire";
import type { HireJob, HireTeamMember } from "@/lib/hire/types";

/**
 * The job's owner recruiter and co-recruiters: the people told about candidate replies,
 * uploads and SLA alerts. Editable with `hire.job.write`; read-only otherwise.
 */
export function JobRecruiters({
  job,
  team,
  canEdit,
  save = setRecruiters,
}: {
  job: HireJob;
  team: HireTeamMember[];
  canEdit: boolean;
  save?: typeof setRecruiters;
}) {
  const t = useTranslations("Hire.recruiters");
  const tErr = useTranslations("Errors");
  const [owner, setOwner] = useState(job.owner_id ?? job.created_by ?? "");
  const [co, setCo] = useState<string[]>(job.co_recruiter_ids);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const name = (id: string) =>
    team.find((m) => m.id === id)?.name ?? t("unknown");

  if (!canEdit) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("summary", {
          owner: owner ? name(owner) : t("none"),
          count: co.length,
        })}
      </p>
    );
  }

  const toggle = (id: string, on: boolean) =>
    setCo((prev) =>
      on ? [...prev.filter((x) => x !== id), id] : prev.filter((x) => x !== id),
    );

  const submit = async () => {
    if (!owner) return;
    setBusy(true);
    setNote(null);
    const result = await save({
      jobId: job.id,
      ownerId: owner,
      coRecruiterIds: co.filter((x) => x !== owner),
    });
    setBusy(false);
    if (result.ok) {
      setOwner(result.job.owner_id ?? owner);
      setCo(result.job.co_recruiter_ids);
      setNote(t("saved"));
    } else {
      setNote(tErr(errorKey(result.code) as "generic"));
    }
  };

  return (
    <details className="rounded-md border border-border px-3 py-2 text-sm">
      <summary className="cursor-pointer font-medium">
        {t("title")}: {owner ? name(owner) : t("none")}
      </summary>
      <div className="mt-3 space-y-3">
        <label className="block space-y-1">
          <span className="block font-medium">{t("owner")}</span>
          <Select
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
            disabled={busy}
          >
            {!owner && <option value="">{t("none")}</option>}
            {team.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </Select>
        </label>
        <fieldset className="space-y-1">
          <legend className="font-medium">{t("co")}</legend>
          <p className="text-xs text-muted-foreground">{t("help")}</p>
          {team
            .filter((m) => m.id !== owner)
            .map((m) => (
              <label key={m.id} className="flex items-center gap-2">
                <Checkbox
                  checked={co.includes(m.id)}
                  disabled={busy || (!co.includes(m.id) && co.length >= 10)}
                  onChange={(e) => toggle(m.id, e.target.checked)}
                />
                <span>{m.name}</span>
              </label>
            ))}
        </fieldset>
        <div className="flex items-center gap-3">
          <Button
            size="sm"
            onClick={() => void submit()}
            disabled={busy || !owner}
          >
            {t("save")}
          </Button>
          {note && (
            <span role="status" className="text-xs text-muted-foreground">
              {note}
            </span>
          )}
        </div>
      </div>
    </details>
  );
}
