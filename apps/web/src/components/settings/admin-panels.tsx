// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Copy, KeyRound, Loader2, RotateCcw } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { FormError } from "@/components/auth/auth-card";
import { EmptyState } from "@/components/states/empty-state";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Checkbox, Select, Switch } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import { createApiKey, resetFlag, revokeApiKey, setFlag } from "@/lib/actions/settings";
import { errorKey } from "@/lib/action-result";
import type { ApiKey, Flag } from "@/lib/api/types";

/** A failed admin action: the step-up prompt for `second_factor_required`, else one plain sentence. */
export function ActionError({ code, returnPath, totpEnabled }: { code: string | null; returnPath: string; totpEnabled: boolean }) {
  const tErrors = useTranslations("Errors");
  const id = useId();
  if (!code) return null;
  if (code === "second_factor_required") return <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} />;
  return <FormError id={id}>{tErrors(errorKey(code) as "generic")}</FormError>;
}

/** A secret the API shows once: shown once here too, with a copy button, never stored. */
export function SecretOnce({ label, secret, onDone }: { label: string; secret: string; onDone: () => void }) {
  const t = useTranslations("SettingsCommon");
  const [copied, setCopied] = useState(false);
  return (
    <div role="status" className="space-y-3 rounded-xl border border-signal/50 bg-signal-soft/60 p-4 text-sm">
      <p className="font-medium">{label}</p>
      <p className="text-muted-foreground">{t("secretOnce")}</p>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <code dir="ltr" className="min-w-0 flex-1 break-all rounded bg-background px-2 py-1.5 font-mono text-xs">
          {secret}
        </code>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(secret);
              setCopied(true);
            } catch {
              setCopied(false);
            }
          }}
        >
          {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
          {copied ? t("copied") : t("copy")}
        </Button>
      </div>
      <Button type="button" size="sm" onClick={onDone}>
        {t("savedIt")}
      </Button>
    </div>
  );
}

// ------------------------------------------------------------------ feature flags

/** Feature flags (`core.flags.read`; changes need `core.flags.manage`). Changes apply at once. */
export function FlagsPanel({
  initial,
  canManage,
  totpEnabled,
  set = setFlag,
  reset = resetFlag,
}: {
  initial: Flag[];
  canManage: boolean;
  totpEnabled: boolean;
  set?: typeof setFlag;
  reset?: typeof resetFlag;
}) {
  const t = useTranslations("FlagsSettings");
  const locale = useLocale();
  const toast = useToast();
  const [flags, setFlags] = useState(initial);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const apply = async (key: string, run: () => ReturnType<typeof setFlag>) => {
    setBusy(key);
    setError(null);
    try {
      const result = await run();
      if (result.ok) {
        setFlags((all) => all.map((f) => (f.key === key ? result.flag : f)));
        toast.show({ tone: "success", title: t("saved", { key }) });
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setBusy(null);
  };

  if (flags.length === 0) return <EmptyState title={t("empty")} />;

  return (
    <div className="space-y-4">
      {!canManage && <p className="text-sm text-muted-foreground">{t("readOnly")}</p>}
      <ActionError code={error} returnPath={`/${locale}/settings/flags`} totpEnabled={totpEnabled} />
      <ul className="divide-y rounded-xl border bg-surface">
        {flags.map((flag) => (
          <li key={flag.key} className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0 space-y-1">
              <p className="font-mono text-sm" dir="ltr">
                {flag.key}
              </p>
              <p className="text-sm text-muted-foreground">{flag.description}</p>
              <p className="text-xs text-muted-foreground">
                {flag.overridden ? t("overridden", { value: flag.default ? t("on") : t("off") }) : t("usingDefault")}
              </p>
            </div>
            <div className="flex items-center gap-3">
              {flag.overridden && canManage && (
                <Button variant="ghost" size="sm" disabled={busy !== null} onClick={() => apply(flag.key, () => reset(flag.key))}>
                  <RotateCcw aria-hidden />
                  {t("reset")}
                </Button>
              )}
              {busy === flag.key && <Loader2 className="size-4 animate-spin text-muted-foreground" aria-hidden />}
              <Switch
                checked={flag.enabled}
                disabled={!canManage || busy !== null}
                onCheckedChange={(enabled) => apply(flag.key, () => set(flag.key, enabled))}
                aria-label={t("toggle", { key: flag.key })}
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ------------------------------------------------------------------ API keys

/** API keys: create (secret shown once), list, revoke. Scopes are limited to read, propose and upload. */
export function ApiKeysPanel({
  initial,
  scopes,
  totpEnabled,
  create = createApiKey,
  revoke = revokeApiKey,
}: {
  initial: ApiKey[];
  /** Permissions a key may hold that the person holds themselves. */
  scopes: string[];
  totpEnabled: boolean;
  create?: typeof createApiKey;
  revoke?: typeof revokeApiKey;
}) {
  const t = useTranslations("ApiKeysSettings");
  const format = useFormatter();
  const locale = useLocale();
  const toast = useToast();
  const ids = { name: useId(), mode: useId(), days: useId(), scopes: useId() };
  const [keys, setKeys] = useState(initial);
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"live" | "test">("test");
  const [days, setDays] = useState(90);
  const [chosen, setChosen] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || chosen.length === 0) return;
    setPending(true);
    setError(null);
    try {
      const result = await create({ name: name.trim(), scopes: chosen, mode, expiresInDays: days });
      if (result.ok) {
        const { key, ...row } = result.key;
        setSecret(key);
        setKeys((all) => [row, ...all]);
        setName("");
        setChosen([]);
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  const confirmRevoke = async () => {
    if (!revoking) return;
    const target = revoking;
    setRevoking(null);
    setError(null);
    try {
      const result = await revoke(target.id);
      if (result.ok) {
        setKeys((all) => all.map((k) => (k.id === target.id ? result.key : k)));
        toast.show({ tone: "success", title: t("revoked", { name: target.name }) });
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
  };

  return (
    <div className="space-y-8">
      {secret && <SecretOnce label={t("created")} secret={secret} onDone={() => setSecret(null)} />}
      <form onSubmit={submit} className="space-y-4 rounded-xl border bg-surface p-5">
        <div className="space-y-1">
          <h2 className="font-display text-xl">{t("createTitle")}</h2>
          <p className="text-sm text-muted-foreground">{t("createBody")}</p>
        </div>
        <fieldset disabled={pending} className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-2 sm:col-span-3">
            <label htmlFor={ids.name} className="text-sm font-medium">
              {t("name")}
            </label>
            <Input id={ids.name} maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-2">
            <label htmlFor={ids.mode} className="text-sm font-medium">
              {t("mode")}
            </label>
            <Select id={ids.mode} value={mode} onChange={(e) => setMode(e.target.value === "live" ? "live" : "test")}>
              <option value="test">{t("modes.test")}</option>
              <option value="live">{t("modes.live")}</option>
            </Select>
          </div>
          <div className="space-y-2">
            <label htmlFor={ids.days} className="text-sm font-medium">
              {t("expires")}
            </label>
            <Select id={ids.days} value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[30, 90, 180, 365].map((d) => (
                <option key={d} value={d}>
                  {t("days", { count: d })}
                </option>
              ))}
            </Select>
          </div>
          <fieldset className="space-y-2 sm:col-span-3" aria-describedby={ids.scopes}>
            <legend className="text-sm font-medium">{t("scopes")}</legend>
            <p id={ids.scopes} className="text-xs text-muted-foreground">
              {t("scopesHint")}
            </p>
            {scopes.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("noScopes")}</p>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2">
                {scopes.map((scope) => (
                  <label key={scope} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={chosen.includes(scope)}
                      onChange={(e) =>
                        setChosen((all) => (e.target.checked ? [...all, scope] : all.filter((s) => s !== scope)))
                      }
                    />
                    <span className="font-mono text-xs" dir="ltr">
                      {scope}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </fieldset>
        </fieldset>
        <ActionError code={error} returnPath={`/${locale}/settings/api-keys`} totpEnabled={totpEnabled} />
        <Button type="submit" disabled={pending || !name.trim() || chosen.length === 0}>
          <KeyRound aria-hidden />
          {pending ? t("creating") : t("create")}
        </Button>
      </form>

      <section aria-labelledby="keys-heading" className="space-y-3">
        <h2 id="keys-heading" className="eyebrow">
          {t("listTitle")}
        </h2>
        {keys.length === 0 ? (
          <EmptyState icon={<KeyRound aria-hidden />} title={t("empty")} />
        ) : (
          <ul className="divide-y rounded-xl border bg-surface">
            {keys.map((key) => {
              const revoked = key.revoked_at !== null;
              const expired = !revoked && new Date(key.expires_at) < new Date();
              return (
                <li key={key.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0 space-y-1">
                    <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                      <bdi>{key.name}</bdi>
                      <Badge variant="outline">{t(`modes.${key.mode === "live" ? "live" : "test"}`)}</Badge>
                      {revoked && <Badge variant="outline">{t("revokedBadge")}</Badge>}
                      {expired && <Badge variant="outline">{t("expiredBadge")}</Badge>}
                    </p>
                    <p className="font-mono text-xs text-muted-foreground" dir="ltr">
                      {key.prefix}…
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {t("meta", {
                        scopes: key.scopes.length,
                        expires: format.dateTime(new Date(key.expires_at), { dateStyle: "medium" }),
                        used: key.last_used_at
                          ? format.relativeTime(new Date(key.last_used_at), new Date())
                          : t("never"),
                      })}
                    </p>
                  </div>
                  {!revoked && (
                    <Button variant="ghost" size="sm" className="text-destructive" onClick={() => setRevoking(key)}>
                      {t("revoke")}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => !open && setRevoking(null)}
        title={t("revokeTitle")}
        description={t("revokeBody", { name: revoking?.name ?? "" })}
        confirmLabel={t("revoke")}
        tone="destructive"
        onConfirm={confirmRevoke}
      />
    </div>
  );
}
