// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ChevronDown, Loader2, RefreshCw, Webhook } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Checkbox, Switch } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import { createWebhook, loadDeliveries, rotateWebhookSecret, setWebhookActive } from "@/lib/actions/settings";
import type { WebhookDelivery, WebhookEndpoint, WebhookEventType } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { ActionError, SecretOnce } from "./admin-panels";

/**
 * Webhook endpoints (`core.webhooks.read`; changes need `core.webhooks.manage`): event
 * types, signing secret (shown once), active switch, delivery log. URLs come from the
 * API already redacted for people who cannot manage webhooks; this page never tries to
 * show more.
 */
export function WebhooksPanel({
  initial,
  eventTypes,
  canManage,
  totpEnabled,
  actions = { create: createWebhook, setActive: setWebhookActive, rotate: rotateWebhookSecret, deliveries: loadDeliveries },
}: {
  initial: WebhookEndpoint[];
  eventTypes: WebhookEventType[];
  canManage: boolean;
  totpEnabled: boolean;
  actions?: {
    create: typeof createWebhook;
    setActive: typeof setWebhookActive;
    rotate: typeof rotateWebhookSecret;
    deliveries: typeof loadDeliveries;
  };
}) {
  const t = useTranslations("WebhooksSettings");
  const locale = useLocale();
  const toast = useToast();
  const ids = { url: useId(), description: useId() };
  const [endpoints, setEndpoints] = useState(initial);
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [rotating, setRotating] = useState<WebhookEndpoint | null>(null);

  const replace = (endpoint: WebhookEndpoint) =>
    setEndpoints((all) => (all.some((e) => e.id === endpoint.id) ? all.map((e) => (e.id === endpoint.id ? endpoint : e)) : [endpoint, ...all]));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!url.trim() || chosen.length === 0) return;
    setPending(true);
    setError(null);
    try {
      const result = await actions.create({ url: url.trim(), description: description.trim() || null, eventTypes: chosen });
      if (result.ok) {
        const { secret: signing, ...endpoint } = result.endpoint;
        setSecret(signing);
        replace(endpoint);
        setUrl("");
        setDescription("");
        setChosen([]);
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  const toggle = async (endpoint: WebhookEndpoint, active: boolean) => {
    setError(null);
    try {
      const result = await actions.setActive(endpoint.id, active);
      if (result.ok) replace(result.endpoint);
      else setError(result.code);
    } catch {
      setError("unavailable");
    }
  };

  const rotate = async () => {
    if (!rotating) return;
    const target = rotating;
    setRotating(null);
    setError(null);
    try {
      const result = await actions.rotate(target.id);
      if (result.ok) {
        const { secret: signing, ...endpoint } = result.endpoint;
        setSecret(signing);
        replace(endpoint);
        toast.show({ tone: "success", title: t("rotated") });
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
  };

  return (
    <div className="space-y-8">
      {secret && <SecretOnce label={t("secretTitle")} secret={secret} onDone={() => setSecret(null)} />}
      <ActionError code={error} returnPath={`/${locale}/settings/webhooks`} totpEnabled={totpEnabled} />
      {canManage ? (
        <form onSubmit={submit} className="space-y-4 rounded-xl border bg-surface p-5">
          <div className="space-y-1">
            <h2 className="font-display text-xl">{t("createTitle")}</h2>
            <p className="text-sm text-muted-foreground">{t("createBody")}</p>
          </div>
          <fieldset disabled={pending} className="space-y-4">
            <div className="space-y-2">
              <label htmlFor={ids.url} className="text-sm font-medium">
                {t("url")}
              </label>
              <Input
                id={ids.url}
                type="url"
                dir="ltr"
                inputMode="url"
                placeholder="https://example.com/hooks/pairly"
                maxLength={2048}
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                className="text-start"
              />
            </div>
            <div className="space-y-2">
              <label htmlFor={ids.description} className="text-sm font-medium">
                {t("description")}
              </label>
              <Input id={ids.description} maxLength={200} value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">{t("events")}</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {eventTypes.map((type) => (
                  <label key={type.key} className="flex items-start gap-2 text-sm">
                    <Checkbox
                      className="mt-0.5"
                      checked={chosen.includes(type.key)}
                      onChange={(e) => setChosen((all) => (e.target.checked ? [...all, type.key] : all.filter((k) => k !== type.key)))}
                    />
                    <span>
                      <span className="block font-mono text-xs" dir="ltr">
                        {type.key}
                      </span>
                      <span className="block text-xs text-muted-foreground">{type.description}</span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
          </fieldset>
          <Button type="submit" disabled={pending || !url.trim() || chosen.length === 0}>
            <Webhook aria-hidden />
            {pending ? t("creating") : t("create")}
          </Button>
        </form>
      ) : (
        <p className="text-sm text-muted-foreground">{t("readOnly")}</p>
      )}

      <section aria-labelledby="endpoints-heading" className="space-y-3">
        <h2 id="endpoints-heading" className="eyebrow">
          {t("listTitle")}
        </h2>
        {endpoints.length === 0 ? (
          <EmptyState icon={<Webhook aria-hidden />} title={t("empty")} />
        ) : (
          <ul className="space-y-3">
            {endpoints.map((endpoint) => (
              <EndpointRow
                key={endpoint.id}
                endpoint={endpoint}
                canManage={canManage}
                onToggle={(active) => toggle(endpoint, active)}
                onRotate={() => setRotating(endpoint)}
                loadDeliveries={actions.deliveries}
              />
            ))}
          </ul>
        )}
      </section>
      <ConfirmDialog
        open={rotating !== null}
        onOpenChange={(open) => !open && setRotating(null)}
        title={t("rotateTitle")}
        description={t("rotateBody")}
        confirmLabel={t("rotate")}
        tone="destructive"
        onConfirm={rotate}
      />
    </div>
  );
}

function EndpointRow({
  endpoint,
  canManage,
  onToggle,
  onRotate,
  loadDeliveries: load,
}: {
  endpoint: WebhookEndpoint;
  canManage: boolean;
  onToggle: (active: boolean) => void;
  onRotate: () => void;
  loadDeliveries: typeof loadDeliveries;
}) {
  const t = useTranslations("WebhooksSettings");
  const format = useFormatter();
  const [open, setOpen] = useState(false);
  const [deliveries, setDeliveries] = useState<WebhookDelivery[] | null>(null);
  const [loading, setLoading] = useState(false);
  const logId = useId();

  const showLog = async () => {
    const next = !open;
    setOpen(next);
    if (next && deliveries === null) {
      setLoading(true);
      try {
        const result = await load(endpoint.id);
        setDeliveries(result.ok ? result.deliveries : []);
      } catch {
        setDeliveries([]);
      }
      setLoading(false);
    }
  };

  return (
    <li className="space-y-3 rounded-xl border bg-surface p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <p className="break-all font-mono text-sm" dir="ltr">
            {endpoint.url}
          </p>
          {endpoint.url_redacted && <p className="text-xs text-muted-foreground">{t("urlRedacted")}</p>}
          {endpoint.description && (
            <p className="text-sm text-muted-foreground">
              <bdi>{endpoint.description}</bdi>
            </p>
          )}
          <div className="flex flex-wrap gap-1.5 pt-1">
            {endpoint.event_types.map((type) => (
              <Badge key={type} variant="outline" className="font-mono">
                {type}
              </Badge>
            ))}
          </div>
          {endpoint.disabled_at && (
            <p className="text-xs text-destructive">
              {t("disabled", { reason: endpoint.disabled_reason ?? t("unknownReason") })}
            </p>
          )}
          {endpoint.consecutive_failures > 0 && (
            <p className="text-xs text-muted-foreground">{t("failures", { count: endpoint.consecutive_failures })}</p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {canManage && (
            <Button variant="ghost" size="sm" onClick={onRotate}>
              <RefreshCw aria-hidden />
              {t("rotate")}
            </Button>
          )}
          <Switch
            checked={endpoint.active}
            disabled={!canManage}
            onCheckedChange={onToggle}
            aria-label={t("activeFor", { url: endpoint.url })}
          />
        </div>
      </div>
      <div>
        <Button variant="ghost" size="sm" aria-expanded={open} aria-controls={logId} onClick={showLog}>
          <ChevronDown className={cn("transition-transform", open && "rotate-180")} aria-hidden />
          {t("deliveries")}
        </Button>
        <div id={logId} hidden={!open} className="pt-2">
          {loading && <Loader2 className="size-4 animate-spin text-muted-foreground" aria-label={t("loading")} />}
          {deliveries?.length === 0 && <p className="text-sm text-muted-foreground">{t("noDeliveries")}</p>}
          {deliveries && deliveries.length > 0 && (
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full min-w-[36rem] text-xs">
                <caption className="sr-only">{t("deliveries")}</caption>
                <thead>
                  <tr className="border-b text-muted-foreground">
                    <th scope="col" className="px-3 py-2 text-start font-medium">{t("event")}</th>
                    <th scope="col" className="px-3 py-2 text-start font-medium">{t("status")}</th>
                    <th scope="col" className="px-3 py-2 text-start font-medium">{t("response")}</th>
                    <th scope="col" className="px-3 py-2 text-start font-medium">{t("when")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {deliveries.map((d) => (
                    <tr key={d.id}>
                      <td className="px-3 py-2 font-mono" dir="ltr">
                        {d.event_type}
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant="outline" className={cn(d.status === "delivered" && "border-success/40 text-success", d.status === "failed" && "border-destructive/40 text-destructive")}>
                          {d.status}
                        </Badge>
                        <span className="ms-2 text-muted-foreground">{t("attempts", { count: d.attempts })}</span>
                      </td>
                      <td className="px-3 py-2 font-mono" dir="ltr">
                        {d.response_code ?? "-"}
                        {d.latency_ms !== null && <span className="text-muted-foreground"> · {d.latency_ms} ms</span>}
                      </td>
                      <td className="px-3 py-2">{format.dateTime(new Date(d.created_at), { dateStyle: "short", timeStyle: "short" })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </li>
  );
}
