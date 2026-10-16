// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { startFairnessRun } from "@/lib/actions/fairness";

/** Starts a fairness run now; the result shows on the next load of the report. */
export function FairnessRunButton({ start = startFairnessRun }: { start?: typeof startFairnessRun }) {
  const t = useTranslations("HireBias.fairness");
  const [state, setState] = useState<"idle" | "busy" | "started" | "failed">("idle");
  async function run() {
    setState("busy");
    const result = await start();
    setState(result.ok ? "started" : "failed");
  }
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <Button size="sm" variant="outline" disabled={state === "busy" || state === "started"} onClick={() => void run()} data-fairness-run>
        {state === "busy" && <Loader2 aria-hidden className="animate-spin" />}
        {t("run")}
      </Button>
      {state === "started" && <span role="status">{t("started")}</span>}
      {state === "failed" && <span role="alert">{t("failed")}</span>}
    </div>
  );
}
