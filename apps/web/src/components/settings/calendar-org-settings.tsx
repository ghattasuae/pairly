// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlertTriangle, CheckCircle2, CircleDashed, Loader2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FieldRow } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Link } from "@/i18n/navigation";
import * as calendarActions from "@/lib/actions/scheduling";
import type { CalendarOperations, OrgIntegration, OrgIntegrations } from "@/lib/hire/scheduling";

const MISSING = new Set(["server_client", "zoom_app", "feature_flag", "people_connect"]);
const PROVIDERS = new Set(["google", "microsoft", "zoom"]);

/**
 * Settings, Calendar, for administrators (`hire.settings.manage`): the organisation's
 * Google, Microsoft 365 and Zoom set-up, the Zoom app form, and the external calendar
 * writes waiting for a person's review.
 *
 * The Zoom client secret goes one way: the form never shows a saved one (the API never
 * returns it) and clears the field after a save.
 */
export function CalendarOrgSettings({
  initial,
  operations,
  actions = calendarActions,
}: {
  initial: OrgIntegrations;
  operations: CalendarOperations | null;
  actions?: Pick<typeof calendarActions, "saveZoom" | "removeZoom">;
}) {
  const t = useTranslations("CalendarSettings.org");
  const tc = useTranslations("CalendarSettings");
  const [org, setOrg] = useState(initial);
  const waiting = org.operations_needing_review;
  const zoom = org.providers.find((p) => p.provider === "zoom") ?? null;

  return (
    <section aria-labelledby="calendar-org" className="space-y-4">
      <div className="space-y-1">
        <h2 id="calendar-org" className="font-display text-2xl">
          {t("title")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("body")}</p>
      </div>
      <p className="text-sm" data-calendar-review={waiting}>
        {waiting > 0 ? (
          <a href="#calendar-review" className="font-medium text-primary underline-offset-4 hover:underline">
            {t("review", { count: waiting })}
          </a>
        ) : (
          t("reviewNone")
        )}
      </p>
      <ul className="grid gap-3 md:grid-cols-3">
        {org.providers
          .filter((p) => PROVIDERS.has(p.provider))
          .map((p) => (
            <li
              key={p.provider}
              className="space-y-2 rounded-xl border bg-surface p-5 shadow-soft"
              data-org-provider={p.provider}
            >
              <div className="flex items-start justify-between gap-2">
                <p className="font-medium">{tc(`name.${p.provider as "google" | "microsoft" | "zoom"}`)}</p>
                <Badge variant={p.state === "connected" ? "default" : "outline"}>
                  {p.state === "connected" ? (
                    <CheckCircle2 className="size-3" aria-hidden />
                  ) : (
                    <CircleDashed className="size-3" aria-hidden />
                  )}
                  {t(p.state === "connected" ? "state.connected" : "state.needs_setup")}
                </Badge>
              </div>
              <OrgFacts provider={p} />
            </li>
          ))}
      </ul>
      {zoom && <ZoomForm zoom={zoom} onSaved={setOrg} actions={actions} />}
      {operations && <ReviewList operations={operations} />}
    </section>
  );
}

function OrgFacts({ provider }: { provider: OrgIntegration }) {
  const t = useTranslations("CalendarSettings.org");
  const missing = provider.missing.filter((m) => MISSING.has(m));
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      <p>{t("flag", { flag: provider.flag_key, state: provider.flag_enabled ? t("on") : t("off") })}</p>
      {provider.connected_people !== null && <p>{t("people", { count: provider.connected_people })}</p>}
      {missing.length > 0 && (
        <>
          <p className="font-medium text-foreground">{t("missingTitle")}</p>
          <ul className="list-disc space-y-0.5 ps-5">
            {missing.map((m) => (
              <li key={m}>{t(`missing.${m as "server_client" | "zoom_app" | "feature_flag" | "people_connect"}`)}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function ZoomForm({
  zoom,
  onSaved,
  actions,
}: {
  zoom: OrgIntegration;
  onSaved: (org: OrgIntegrations) => void;
  actions: Pick<typeof calendarActions, "saveZoom" | "removeZoom">;
}) {
  const t = useTranslations("CalendarSettings.org.zoom");
  const format = useFormatter();
  const ids = useId();
  const [accountId, setAccountId] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [host, setHost] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  return (
    <form
      aria-labelledby={`${ids}-title`}
      className="space-y-4 rounded-xl border bg-surface p-5 shadow-soft"
      data-zoom-form
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        const result = await actions.saveZoom({ accountId, clientId, clientSecret, host });
        setBusy(false);
        // The secret is never kept in the page once sent.
        setClientSecret("");
        if (result.ok) {
          onSaved(result.org);
          setStatus(t("saved"));
        } else setStatus(t(result.code === "invalid_input" ? "invalid" : "failed"));
      }}
    >
      <div className="space-y-1">
        <h3 id={`${ids}-title`} className="font-medium">
          {t("title")}
        </h3>
        <p className="text-xs text-muted-foreground">{t("help")}</p>
        {zoom.configured ? (
          <p className="text-xs" data-zoom-saved>
            {zoom.verified_at
              ? t("savedAt", { date: format.dateTime(new Date(zoom.verified_at), { dateStyle: "medium" }) })
              : t("savedOnly")}
          </p>
        ) : (
          <p className="text-xs">{t("none")}</p>
        )}
        {zoom.last_error && (
          <p className="flex items-center gap-1 text-xs text-destructive">
            <AlertTriangle className="size-3" aria-hidden />
            {t("lastError", { code: zoom.last_error })}
          </p>
        )}
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <FieldRow label={t("accountId")} htmlFor={`${ids}-account`}>
          <Input id={`${ids}-account`} value={accountId} onChange={(e) => setAccountId(e.target.value)} required maxLength={128} autoComplete="off" dir="ltr" />
        </FieldRow>
        <FieldRow label={t("clientId")} htmlFor={`${ids}-client`}>
          <Input id={`${ids}-client`} value={clientId} onChange={(e) => setClientId(e.target.value)} required maxLength={128} autoComplete="off" dir="ltr" />
        </FieldRow>
        <FieldRow label={t("clientSecret")} htmlFor={`${ids}-secret`}>
          <Input
            id={`${ids}-secret`}
            type="password"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            required
            maxLength={128}
            autoComplete="new-password"
            placeholder={zoom.configured ? t("secretKept") : undefined}
            dir="ltr"
          />
        </FieldRow>
        <FieldRow label={t("host")} htmlFor={`${ids}-host`}>
          <Input id={`${ids}-host`} type="email" value={host} onChange={(e) => setHost(e.target.value)} maxLength={254} autoComplete="off" dir="ltr" />
        </FieldRow>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={busy}>
          {busy && <Loader2 className="animate-spin" aria-hidden />}
          {t(zoom.configured ? "replace" : "save")}
        </Button>
        {zoom.configured && (
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              const result = await actions.removeZoom();
              setBusy(false);
              if (result.ok) {
                onSaved(result.org);
                setStatus(t("removed"));
              } else setStatus(t("failed"));
            }}
          >
            {t("remove")}
          </Button>
        )}
        {status && (
          <p role="status" className="text-sm">
            {status}
          </p>
        )}
      </div>
    </form>
  );
}

function ReviewList({ operations }: { operations: CalendarOperations }) {
  const t = useTranslations("CalendarSettings.org.reviewList");
  const format = useFormatter();
  return (
    <div id="calendar-review" className="scroll-mt-24 space-y-2" data-calendar-review-list>
      <h3 className="font-medium">{t("title")}</h3>
      {operations.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("empty")}</p>
      ) : (
        <ul className="divide-y rounded-xl border bg-surface">
          {operations.items.map((op) => (
            <li key={op.id} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
              <div className="space-y-0.5">
                <p className="font-medium">
                  {t("what", {
                    provider: op.provider,
                    action: t(`action.${(["create", "update", "cancel"].includes(op.action) ? op.action : "other") as "create"}`),
                  })}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t(op.rejected ? "refused" : "unknown")} {op.last_error ? `(${op.last_error})` : ""}
                  {" · "}
                  {format.dateTime(new Date(op.updated_at), { dateStyle: "medium", timeStyle: "short" })}
                </p>
              </div>
              {op.job_id && op.application_id ? (
                <Link
                  href={`/hire/jobs/${op.job_id}/pipeline?candidate=${op.application_id}`}
                  className="text-sm font-medium text-primary underline-offset-4 hover:underline"
                >
                  {t("open")}
                </Link>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
