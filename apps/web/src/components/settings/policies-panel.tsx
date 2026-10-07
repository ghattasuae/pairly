// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { FileUp, Loader2, Play, ScrollText, ShieldAlert } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import {
  archivePolicyDocument,
  dryRunRuleSet,
  indexPolicyDocument,
  loadRuleSetVersion,
  loadRuleSetVersions,
  overridePolicyDocument,
  uploadPolicyDocument,
} from "@/lib/actions/settings";
import type { DryRun, PolicyDocument, RuleSetVersion } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { ActionError } from "./admin-panels";

const RECENT_KEY = "pairly.recentRuleSets";

export interface PoliciesPanelProps {
  documents: PolicyDocument[];
  meId: string;
  canManage: boolean;
  totpEnabled: boolean;
  actions?: {
    upload: typeof uploadPolicyDocument;
    override: typeof overridePolicyDocument;
    index: typeof indexPolicyDocument;
    archive: typeof archivePolicyDocument;
    versions: typeof loadRuleSetVersions;
    version: typeof loadRuleSetVersion;
    dryRun: typeof dryRunRuleSet;
  };
}

const DEFAULT_ACTIONS = {
  upload: uploadPolicyDocument,
  override: overridePolicyDocument,
  index: indexPolicyDocument,
  archive: archivePolicyDocument,
  versions: loadRuleSetVersions,
  version: loadRuleSetVersion,
  dryRun: dryRunRuleSet,
};

function StatusBadge({ status }: { status: string }) {
  const t = useTranslations("PoliciesSettings.status");
  const known = ["pending", "indexed", "failed", "needs_review", "archived"].includes(status);
  return (
    <Badge
      variant={status === "needs_review" ? "signal" : "outline"}
      className={cn(status === "indexed" && "border-success/40 text-success", status === "failed" && "border-destructive/40 text-destructive")}
    >
      {known ? t(status as "pending") : status}
    </Badge>
  );
}

/**
 * Policies (brief 5.2.5): policy documents for policy checks (upload a new version, see
 * guardrail warnings, override with a written reason, two admins for high-severity
 * findings) and rule sets (versions, dry run on sample facts).
 */
export function PoliciesPanel({ documents, meId, canManage, totpEnabled, actions = DEFAULT_ACTIONS }: PoliciesPanelProps) {
  const t = useTranslations("PoliciesSettings");
  const locale = useLocale();
  const [docs, setDocs] = useState(documents);
  const [error, setError] = useState<string | null>(null);
  const returnPath = `/${locale}/settings/policies`;

  const replace = (doc: PolicyDocument) =>
    setDocs((all) => (all.some((d) => d.id === doc.id) ? all.map((d) => (d.id === doc.id ? doc : d)) : [doc, ...all]));

  return (
    <div className="space-y-10">
      <ActionError code={error} returnPath={returnPath} totpEnabled={totpEnabled} />
      {canManage && <UploadForm upload={actions.upload} onDone={replace} onError={setError} />}

      <section aria-labelledby="policy-docs" className="space-y-3">
        <h2 id="policy-docs" className="eyebrow">
          {t("documentsTitle")}
        </h2>
        {docs.length === 0 ? (
          <EmptyState icon={<ScrollText aria-hidden />} title={t("empty")} body={t("emptyBody")} />
        ) : (
          <ul className="space-y-3">
            {docs.map((doc) => (
              <PolicyRow
                key={doc.id}
                doc={doc}
                meId={meId}
                canManage={canManage}
                actions={actions}
                onChange={replace}
                onError={setError}
              />
            ))}
          </ul>
        )}
      </section>

      <RuleSets actions={actions} onError={setError} />
    </div>
  );
}

function UploadForm({
  upload,
  onDone,
  onError,
}: {
  upload: typeof uploadPolicyDocument;
  onDone: (doc: PolicyDocument) => void;
  onError: (code: string | null) => void;
}) {
  const t = useTranslations("PoliciesSettings");
  const toast = useToast();
  const ids = { name: useId(), title: useId(), file: useId(), hint: useId() };
  const [pending, setPending] = useState(false);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    setPending(true);
    onError(null);
    try {
      const result = await upload(new FormData(form));
      if (result.ok) {
        onDone(result.policy);
        form.reset();
        toast.show({ tone: "success", title: t("uploaded"), body: t("uploadedBody") });
      } else onError(result.code);
    } catch {
      onError("unavailable");
    }
    setPending(false);
  };

  return (
    <form onSubmit={submit} className="space-y-4 rounded-xl border bg-surface p-5">
      <div className="space-y-1">
        <h2 className="font-display text-xl">{t("uploadTitle")}</h2>
        <p className="text-sm text-muted-foreground">{t("uploadBody")}</p>
      </div>
      <fieldset disabled={pending} className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <label htmlFor={ids.name} className="text-sm font-medium">
            {t("name")}
          </label>
          <Input
            id={ids.name}
            name="name"
            dir="ltr"
            required
            pattern="[a-z][a-z0-9_.\-]{0,127}"
            placeholder="travel-policy"
            aria-describedby={ids.hint}
            className="text-start"
          />
          <p id={ids.hint} className="text-xs text-muted-foreground">
            {t("nameHint")}
          </p>
        </div>
        <div className="space-y-2">
          <label htmlFor={ids.title} className="text-sm font-medium">
            {t("docTitle")}
          </label>
          <Input id={ids.title} name="title" required maxLength={200} />
        </div>
        <div className="space-y-2 sm:col-span-2">
          <label htmlFor={ids.file} className="text-sm font-medium">
            {t("file")}
          </label>
          <Input
            id={ids.file}
            name="file"
            type="file"
            required
            accept=".pdf,.docx,.txt,.md,application/pdf,text/plain"
            className="h-auto py-2"
          />
        </div>
      </fieldset>
      <Button type="submit" disabled={pending}>
        {pending ? <Loader2 className="animate-spin" aria-hidden /> : <FileUp aria-hidden />}
        {pending ? t("uploading") : t("upload")}
      </Button>
    </form>
  );
}

function PolicyRow({
  doc,
  meId,
  canManage,
  actions,
  onChange,
  onError,
}: {
  doc: PolicyDocument;
  meId: string;
  canManage: boolean;
  actions: NonNullable<PoliciesPanelProps["actions"]>;
  onChange: (doc: PolicyDocument) => void;
  onError: (code: string | null) => void;
}) {
  const t = useTranslations("PoliciesSettings");
  const format = useFormatter();
  const locale = useLocale();
  const toast = useToast();
  const reasonId = useId();
  const [overriding, setOverriding] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const requestedByMe = doc.override_requested_by === meId;
  const awaitingSecond = !!doc.override_requested_at && !doc.overridden_at;

  const run = async <T,>(fn: () => Promise<{ ok: true } & T | { ok: false; code: string }>, done: (value: T) => void) => {
    setBusy(true);
    onError(null);
    try {
      const result = await fn();
      if (result.ok) done(result as T);
      else onError(result.code);
    } catch {
      onError("unavailable");
    }
    setBusy(false);
  };

  return (
    <li id={`policy-${doc.id}`} className="scroll-mt-20 space-y-3 rounded-xl border bg-surface p-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <p className="font-medium">
            <bdi>{doc.title}</bdi>
          </p>
          <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="font-mono" dir="ltr">
              {doc.name}
            </span>
            <span>v{doc.version}</span>
            <StatusBadge status={doc.status} />
            <span>{format.dateTime(new Date(doc.created_at), { dateStyle: "medium" })}</span>
            {doc.status === "indexed" && <span>{t("chunks", { count: doc.chunk_count })}</span>}
          </p>
          {doc.status_reason && <p className="text-xs text-muted-foreground">{doc.status_reason}</p>}
        </div>
        {canManage && doc.status !== "archived" && (
          <div className="flex flex-wrap gap-2">
            {(doc.status === "pending" || doc.status === "failed") && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  run(
                    () => actions.index(doc.id),
                    (r: { status: string }) => toast.show({ tone: "info", title: t("indexResult", { status: r.status }) }),
                  )
                }
              >
                {t("index")}
              </Button>
            )}
            {doc.status === "needs_review" && !(awaitingSecond && requestedByMe) && (
              <Button size="sm" variant="signal" disabled={busy} onClick={() => setOverriding(true)}>
                {awaitingSecond ? t("approveOverride") : t("override")}
              </Button>
            )}
            <Button size="sm" variant="ghost" className="text-destructive" disabled={busy} onClick={() => setArchiving(true)}>
              {t("archive")}
            </Button>
          </div>
        )}
      </div>

      {doc.warning_count > 0 && (
        <div className="space-y-2">
          <p className="flex items-center gap-2 text-sm font-medium">
            <ShieldAlert className="size-4 text-destructive" aria-hidden />
            {t("warnings", { count: doc.warning_count })}
          </p>
          {doc.warnings.length > 0 ? (
            <ul className="space-y-2">
              {doc.warnings.map((w) => (
                <li key={w.id} className="rounded-lg border border-dashed border-destructive/50 px-3 py-2 text-sm">
                  <p>{locale === "ar" && w.explanation_ar ? w.explanation_ar : w.explanation}</p>
                  <p className="mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                    <span>{t("severity", { severity: w.severity })}</span>
                    {w.page !== null && <span>{t("page", { page: w.page })}</span>}
                    {w.blocks_activation && <span className="text-destructive">{t("blocks")}</span>}
                    {w.excluded && <span>{t("excluded")}</span>}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">{t("warningsHidden")}</p>
          )}
        </div>
      )}

      {awaitingSecond && (
        <p className="rounded-md bg-signal-soft/60 px-3 py-2 text-sm">
          {requestedByMe ? t("awaitingOther") : t("awaitingYou")}
          {doc.override_request_reason && (
            <span className="mt-1 block text-muted-foreground">
              <bdi>{doc.override_request_reason}</bdi>
            </span>
          )}
        </p>
      )}
      {doc.overridden_at && (
        <p className="text-xs text-muted-foreground">
          {t("overriddenOn", { when: format.dateTime(new Date(doc.overridden_at), { dateStyle: "medium" }) })}
        </p>
      )}

      <ConfirmDialog
        open={overriding}
        onOpenChange={setOverriding}
        title={awaitingSecond ? t("approveOverrideTitle") : t("overrideTitle")}
        description={t("overrideBody")}
        confirmLabel={awaitingSecond ? t("approveOverride") : t("override")}
        confirmDisabled={reason.trim().length < 10}
        pending={busy}
        onConfirm={() => {
          setOverriding(false);
          void run(
            () => actions.override({ id: doc.id, reason }),
            (r: { status: string; secondAdminRequired: boolean; policy: PolicyDocument }) => {
              onChange(r.policy);
              setReason("");
              toast.show({
                tone: "success",
                title: r.status === "requested" ? t("overrideRequested") : t("overrideDone"),
                body: r.status === "requested" && r.secondAdminRequired ? t("secondAdmin") : undefined,
              });
            },
          );
        }}
      >
        <div className="space-y-2">
          <label htmlFor={reasonId} className="text-sm font-medium">
            {t("overrideReason")}
          </label>
          <Textarea id={reasonId} autoFocus minLength={10} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
          <p className="text-xs text-muted-foreground">{t("overrideReasonHint")}</p>
        </div>
      </ConfirmDialog>
      <ConfirmDialog
        open={archiving}
        onOpenChange={setArchiving}
        title={t("archiveTitle")}
        description={t("archiveBody", { title: doc.title })}
        confirmLabel={t("archive")}
        tone="destructive"
        onConfirm={() => {
          setArchiving(false);
          void run(() => actions.archive(doc.id), (r: { policy: PolicyDocument }) => onChange(r.policy));
        }}
      />
    </li>
  );
}

function RuleSets({
  actions,
  onError,
}: {
  actions: NonNullable<PoliciesPanelProps["actions"]>;
  onError: (code: string | null) => void;
}) {
  const t = useTranslations("PoliciesSettings.rules");
  const format = useFormatter();
  const locale = useLocale();
  const ids = { name: useId(), facts: useId(), version: useId() };
  const [name, setName] = useState("");
  const [recent, setRecent] = useState<string[]>([]);
  const [opened, setOpened] = useState<string | null>(null);
  const [versions, setVersions] = useState<RuleSetVersion[] | null>(null);
  const [content, setContent] = useState<{ version: number; text: string } | null>(null);
  const [facts, setFacts] = useState("{\n  \n}");
  const [dryVersion, setDryVersion] = useState<number | null>(null);
  const [result, setResult] = useState<DryRun | null>(null);
  const [busy, setBusy] = useState(false);

  // Recently opened names, read when the field is first focused (a per-browser convenience).
  const loadRecent = () => {
    if (recent.length > 0) return;
    try {
      const saved = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]") as unknown;
      if (Array.isArray(saved)) setRecent(saved.filter((s): s is string => typeof s === "string").slice(0, 8));
    } catch {
      /* a convenience only */
    }
  };

  const open = async (target: string) => {
    const clean = target.trim();
    if (!/^[a-z][a-z0-9_.]{0,127}$/.test(clean)) {
      onError("invalid_input");
      return;
    }
    setBusy(true);
    onError(null);
    setResult(null);
    setContent(null);
    try {
      const found = await actions.versions(clean);
      if (found.ok) {
        setOpened(clean);
        setVersions(found.versions);
        setDryVersion(found.versions[0]?.version ?? null);
        const next = [clean, ...recent.filter((r) => r !== clean)].slice(0, 8);
        setRecent(next);
        try {
          localStorage.setItem(RECENT_KEY, JSON.stringify(next));
        } catch {
          /* a convenience only */
        }
      } else {
        setVersions(null);
        setOpened(null);
        onError(found.code);
      }
    } catch {
      onError("unavailable");
    }
    setBusy(false);
  };

  const show = async (version: number) => {
    if (!opened) return;
    const found = await actions.version(opened, version).catch(() => null);
    if (found?.ok) setContent({ version, text: found.version.content ?? "" });
    else onError(found?.ok === false ? found.code : "unavailable");
  };

  const dryRun = async () => {
    if (!opened) return;
    setBusy(true);
    onError(null);
    try {
      const run = await actions.dryRun({ name: opened, version: dryVersion, facts });
      if (run.ok) setResult(run.result);
      else onError(run.code === "invalid_json" ? "invalid_input" : run.code);
    } catch {
      onError("unavailable");
    }
    setBusy(false);
  };

  return (
    <section aria-labelledby="rule-sets" className="space-y-4">
      <div className="space-y-1">
        <h2 id="rule-sets" className="eyebrow">
          {t("title")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("body")}</p>
      </div>
      <form
        className="flex flex-col gap-2 sm:flex-row sm:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          void open(name);
        }}
      >
        <div className="flex-1 space-y-2">
          <label htmlFor={ids.name} className="text-sm font-medium">
            {t("name")}
          </label>
          <Input
            id={ids.name}
            dir="ltr"
            list={`${ids.name}-recent`}
            onFocus={loadRecent}
            placeholder="expense.limits"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="text-start"
          />
          <datalist id={`${ids.name}-recent`}>
            {recent.map((r) => (
              <option key={r} value={r} />
            ))}
          </datalist>
        </div>
        <Button type="submit" variant="outline" disabled={busy || !name.trim()}>
          {t("open")}
        </Button>
      </form>

      {opened && versions && (
        <div className="space-y-4 rounded-xl border bg-surface p-5">
          <h3 className="font-mono text-sm" dir="ltr">
            {opened}
          </h3>
          {versions.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("noVersions")}</p>
          ) : (
            <ul className="divide-y rounded-lg border">
              {versions.map((v) => (
                <li key={v.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                  <span className="flex items-center gap-2">
                    <Badge variant="outline">v{v.version}</Badge>
                    <span className="text-muted-foreground">
                      {format.dateTime(new Date(v.created_at), { dateStyle: "medium", timeStyle: "short" })}
                    </span>
                    <span className="font-mono text-xs text-muted-foreground" dir="ltr">
                      {v.content_hash.slice(0, 12)}
                    </span>
                  </span>
                  <Button variant="ghost" size="sm" onClick={() => void show(v.version)}>
                    {t("view")}
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {content && (
            <figure className="space-y-1">
              <figcaption className="text-xs text-muted-foreground">{t("versionContent", { version: content.version })}</figcaption>
              <pre dir="ltr" className="max-h-80 overflow-auto rounded-lg bg-surface-sunken p-3 font-mono text-xs">
                {content.text}
              </pre>
            </figure>
          )}

          {versions.length > 0 && (
            <div className="space-y-3 border-t pt-4">
              <h4 className="font-medium">{t("dryRunTitle")}</h4>
              <p className="text-sm text-muted-foreground">{t("dryRunBody")}</p>
              <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
                <div className="space-y-2">
                  <label htmlFor={ids.version} className="text-sm font-medium">
                    {t("version")}
                  </label>
                  <Select id={ids.version} value={dryVersion ?? ""} onChange={(e) => setDryVersion(Number(e.target.value) || null)}>
                    {versions.map((v) => (
                      <option key={v.id} value={v.version}>
                        v{v.version}
                      </option>
                    ))}
                  </Select>
                </div>
                <div className="space-y-2">
                  <label htmlFor={ids.facts} className="text-sm font-medium">
                    {t("facts")}
                  </label>
                  <Textarea id={ids.facts} dir="ltr" spellCheck={false} value={facts} onChange={(e) => setFacts(e.target.value)} className="font-mono text-xs" />
                </div>
              </div>
              <Button variant="outline" onClick={dryRun} disabled={busy}>
                {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Play aria-hidden />}
                {t("run")}
              </Button>
              {result && (
                <div role="status" className="space-y-2">
                  <p className="text-sm">
                    {t("overall", { overall: result.overall })}{" "}
                    {result.requires_human && <Badge variant="signal">{t("requiresHuman")}</Badge>}
                  </p>
                  <ul className="divide-y rounded-lg border text-sm">
                    {result.results.map((r) => (
                      <li key={r.rule_id} className="space-y-1 px-3 py-2">
                        <p className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-xs" dir="ltr">
                            {r.rule_id}
                          </span>
                          <Badge
                            variant="outline"
                            className={cn(r.outcome === "pass" && "border-success/40 text-success", r.outcome === "fail" && "border-destructive/40 text-destructive")}
                          >
                            {r.outcome}
                          </Badge>
                        </p>
                        <p className="text-muted-foreground">{locale === "ar" && r.reason_ar ? r.reason_ar : r.reason}</p>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
