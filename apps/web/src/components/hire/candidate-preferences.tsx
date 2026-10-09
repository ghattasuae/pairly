// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { Switch } from "@/components/ui/form-controls";
import { readCandidatePreferences, saveCandidatePreferences, type PrefsView } from "@/lib/actions/candidate-prefs";

/**
 * `/<locale>/candidate/preferences#<token>`: a candidate chooses whether we send them
 * status updates by email, and whether we may contact them about future roles. The token
 * stays in the fragment (never sent to a server by the browser); the page posts it in a
 * JSON body. Outcomes of an application are always sent, and the page says so.
 */
export function CandidatePreferences({
  read = readCandidatePreferences,
  save = saveCandidatePreferences,
}: {
  read?: typeof readCandidatePreferences;
  save?: typeof saveCandidatePreferences;
}) {
  const t = useTranslations("CandidatePrefs");
  const [token, setToken] = useState<string | null>(null);
  const [view, setView] = useState<PrefsView | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const raw = window.location.hash.replace(/^#/, "");
    if (raw) window.history.replaceState(null, "", window.location.pathname);
    let cancelled = false;
    (raw ? read(raw) : Promise.resolve<PrefsView>({ kind: "invalid" }))
      .then((v) => {
        if (cancelled) return;
        setToken(raw);
        setView(v);
      })
      .catch(() => !cancelled && setView({ kind: "unavailable" }));
    return () => {
      cancelled = true;
    };
  }, [read]);

  if (view === null) return null;
  if (view.kind !== "ready") {
    return (
      <AuthCard
        title={view.kind === "invalid" ? t("invalidTitle") : t("unavailableTitle")}
        subtitle={view.kind === "invalid" ? t("invalid") : t("unavailable")}
      >
        {null}
      </AuthCard>
    );
  }

  const change = async (patch: { updates_email?: boolean; talent_pool?: boolean }) => {
    if (!token) return;
    setBusy(true);
    setSaved(false);
    const next = await save(token, patch).catch((): PrefsView => ({ kind: "unavailable" }));
    setBusy(false);
    setView(next);
    setSaved(next.kind === "ready");
  };

  const { prefs } = view;
  return (
    <AuthCard title={t("title")} subtitle={t("subtitle", { organisation: prefs.organisation })}>
      <div className="space-y-5 text-sm">
        <label className="flex items-start justify-between gap-4">
          <span className="space-y-1">
            <span className="block font-medium">{t("updates")}</span>
            <span className="block text-muted-foreground">{t("updatesHelp")}</span>
          </span>
          <Switch
            checked={prefs.updates_email}
            disabled={busy}
            onCheckedChange={(on) => void change({ updates_email: on })}
            aria-label={t("updates")}
          />
        </label>
        <label className="flex items-start justify-between gap-4">
          <span className="space-y-1">
            <span className="block font-medium">{t("talentPool")}</span>
            <span className="block text-muted-foreground">{t("talentPoolHelp")}</span>
          </span>
          <Switch
            checked={prefs.talent_pool}
            disabled={busy}
            onCheckedChange={(on) => void change({ talent_pool: on })}
            aria-label={t("talentPool")}
          />
        </label>
        <p className="text-xs text-muted-foreground">{t("outcomes")}</p>
        {prefs.jobs.length > 0 && (
          <p className="text-xs text-muted-foreground">{t("jobs", { jobs: prefs.jobs.join(", ") })}</p>
        )}
        {saved && (
          <p role="status" className="text-sm font-medium">
            {t("saved")}
          </p>
        )}
      </div>
    </AuthCard>
  );
}
