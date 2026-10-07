// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ChevronDown, Download, Link2, Loader2, Search, ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useMemo, useRef, useState, useTransition } from "react";
import type * as React from "react";

import { FormError } from "@/components/auth/auth-card";
import { JsonView } from "@/components/data/json-view";
import { EmptyState } from "@/components/states/empty-state";
import { PageHeader } from "@/components/states/page-header";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/form-controls";
import { Link, useRouter } from "@/i18n/navigation";
import { chainJob, type ChainStatus, verifyChain } from "@/lib/actions/audit";
import { errorKey } from "@/lib/action-result";
import type { Anchor, AuditEvent } from "@/lib/api/types";
import {
  ACTOR_TYPES,
  type AuditFilters,
  auditQuery,
  EMPTY_AUDIT_FILTERS,
  hasAuditFilters,
} from "@/lib/audit-filters";
import { cn } from "@/lib/utils";

export interface AuditViewProps {
  filters: AuditFilters;
  events: AuditEvent[];
  nextCursor: string | null;
  /** Showing an older page (a cursor is in the URL). */
  paged: boolean;
  /** The API refused the filters (for example a malformed date). */
  invalidFilter: boolean;
  anchors: Anchor[] | null;
  canExport: boolean;
  totpEnabled: boolean;
  /** Overrides for stories and tests. */
  verify?: typeof verifyChain;
  poll?: typeof chainJob;
  download?: (format: "csv" | "json", query: string) => Promise<{ ok: true } | { ok: false; code: string }>;
}

/** Fetch the export through our own route and save it. The browser never sees an API token. */
async function defaultDownload(format: "csv" | "json", query: string) {
  const params = new URLSearchParams(query.replace(/^\?/, ""));
  params.set("format", format);
  const response = await fetch(`/api/audit/export?${params.toString()}`, {
    headers: { "X-Pairly-Export": "1" },
    credentials: "same-origin",
  });
  if (!response.ok) {
    let code = "unavailable";
    try {
      code = ((await response.json()) as { code?: string }).code ?? code;
    } catch {
      /* not JSON */
    }
    return { ok: false as const, code };
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `audit-${new Date().toISOString().slice(0, 10)}.${format}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return { ok: true as const };
}

function FilterForm({ filters }: { filters: AuditFilters }) {
  const t = useTranslations("Audit.filters");
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [draft, setDraft] = useState(filters);
  const [open, setOpen] = useState(hasAuditFilters(filters));
  const id = useId();
  const set = (patch: Partial<AuditFilters>) => setDraft((d) => ({ ...d, ...patch }));
  const field = (key: keyof AuditFilters, label: string, props: React.ComponentProps<typeof Input> = {}) => (
    <div className="space-y-1.5">
      <label htmlFor={`${id}-${key}`} className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      <Input
        id={`${id}-${key}`}
        dir="ltr"
        value={draft[key]}
        onChange={(e) => set({ [key]: e.target.value } as Partial<AuditFilters>)}
        className="h-9"
        {...props}
      />
    </div>
  );

  return (
    <form
      role="search"
      aria-label={t("label")}
      onSubmit={(event) => {
        event.preventDefault();
        startTransition(() => router.push(`/audit${auditQuery(draft)}`));
      }}
      className="rounded-xl border bg-surface"
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={`${id}-fields`}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-2 px-4 py-3 text-sm font-medium"
      >
        <span className="flex items-center gap-2">
          <Search className="size-4 text-muted-foreground" aria-hidden />
          {t("title")}
          {hasAuditFilters(filters) && <Badge variant="signal">{t("active")}</Badge>}
        </span>
        <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      <div id={`${id}-fields`} hidden={!open} className="space-y-4 border-t px-4 py-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {field("action", t("action"), { placeholder: "core.approval.decided" })}
          <div className="space-y-1.5">
            <label htmlFor={`${id}-actorType`} className="text-xs font-medium text-muted-foreground">
              {t("actorType")}
            </label>
            <Select
              id={`${id}-actorType`}
              value={draft.actorType}
              onChange={(e) => set({ actorType: e.target.value as AuditFilters["actorType"] })}
              className="h-9"
            >
              <option value="">{t("any")}</option>
              {ACTOR_TYPES.map((a) => (
                <option key={a} value={a}>
                  {t(`actorTypes.${a}`)}
                </option>
              ))}
            </Select>
          </div>
          {field("actorId", t("actorId"))}
          {field("workflow", t("workflow"))}
          {field("entityType", t("entityType"))}
          {field("entityId", t("entityId"))}
          {field("requestId", t("requestId"))}
          <div className="space-y-1.5">
            <label htmlFor={`${id}-order`} className="text-xs font-medium text-muted-foreground">
              {t("order")}
            </label>
            <Select
              id={`${id}-order`}
              value={draft.order}
              onChange={(e) => set({ order: e.target.value === "asc" ? "asc" : "desc" })}
              className="h-9"
            >
              <option value="desc">{t("newest")}</option>
              <option value="asc">{t("oldest")}</option>
            </Select>
          </div>
          {field("from", t("from"), { type: "date" })}
          {field("to", t("to"), { type: "date" })}
        </div>
        <p className="text-xs text-muted-foreground">{t("hint")}</p>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm">
            {t("apply")}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              setDraft(EMPTY_AUDIT_FILTERS);
              startTransition(() => router.push("/audit"));
            }}
          >
            {t("clear")}
          </Button>
        </div>
      </div>
    </form>
  );
}

function ChainCard({
  anchors,
  verify,
  poll,
}: {
  anchors: Anchor[] | null;
  verify: typeof verifyChain;
  poll: typeof chainJob;
}) {
  const t = useTranslations("Audit.chain");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const [status, setStatus] = useState<ChainStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  const follow = (jobId: string, attempt = 0) => {
    timer.current = window.setTimeout(async () => {
      const result = await poll(jobId).catch(() => null);
      if (!result || !result.ok) {
        setError(result?.ok === false ? result.code : "unavailable");
        setPending(false);
        return;
      }
      setStatus(result.status);
      if (result.status.state === "queued" && attempt < 60) follow(jobId, attempt + 1);
      else setPending(false);
    }, 2000);
  };

  const run = async () => {
    setPending(true);
    setError(null);
    try {
      const result = await verify();
      if (!result.ok) {
        setError(result.code);
        setPending(false);
        return;
      }
      setStatus(result.status);
      if (result.status.state === "queued") follow(result.status.jobId);
      else setPending(false);
    } catch {
      setError("unavailable");
      setPending(false);
    }
  };

  const check = status?.state === "done" ? status.check : null;
  const latest = anchors?.[0] ?? null;

  return (
    <section aria-labelledby="chain-heading" className="space-y-4 rounded-xl border bg-surface p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 id="chain-heading" className="font-display text-xl">
            {t("title")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">{t("body")}</p>
        </div>
        <Button size="sm" onClick={run} disabled={pending}>
          {pending ? <Loader2 className="animate-spin" aria-hidden /> : <ShieldCheck aria-hidden />}
          {pending ? t("checking") : t("check")}
        </Button>
      </div>
      <div aria-live="polite" className="text-sm">
        {status === null && !error && (
          <p className="flex items-center gap-2 text-muted-foreground">
            <ShieldQuestion className="size-4" aria-hidden />
            {t("notChecked")}
          </p>
        )}
        {status?.state === "queued" && (
          <p className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {t("queued")}
          </p>
        )}
        {status?.state === "failed" && <p className="text-destructive">{t("failed")}</p>}
        {check && check.ok && (
          <p className="flex items-center gap-2 text-success">
            <ShieldCheck className="size-4" aria-hidden />
            {t("ok", { count: check.checked, seq: check.last_seq })}
            {check.anchors_checked && <span className="text-muted-foreground">· {t("anchorsOk")}</span>}
          </p>
        )}
        {check && !check.ok && (
          <p className="flex items-center gap-2 text-destructive">
            <ShieldAlert className="size-4" aria-hidden />
            {check.broken_seq !== null ? t("broken", { seq: check.broken_seq }) : t("brokenNoSeq")}
            {check.reason && <span className="font-mono text-xs">({check.reason})</span>}
          </p>
        )}
        {error && <FormError id="chain-error">{tErrors(errorKey(error) as "generic")}</FormError>}
      </div>
      <div className="space-y-2 border-t pt-4 text-sm">
        <h3 className="flex items-center gap-2 font-medium">
          <Link2 className="size-4 text-muted-foreground" aria-hidden />
          {t("anchors")}
        </h3>
        {anchors === null ? (
          <p className="text-muted-foreground">{t("anchorsUnavailable")}</p>
        ) : latest === null ? (
          <p className="text-muted-foreground">{t("noAnchors")}</p>
        ) : (
          <>
            <p className="text-muted-foreground">
              {t("latestAnchor", {
                seq: latest.seq,
                when: format.dateTime(new Date(latest.anchored_at), { dateStyle: "medium", timeStyle: "short" }),
              })}
            </p>
            <details className="text-xs">
              <summary className="cursor-pointer text-primary">{t("allAnchors", { count: anchors.length })}</summary>
              <ul className="mt-2 space-y-1 font-mono" dir="ltr">
                {anchors.slice(0, 30).map((a) => (
                  <li key={`${a.seq}:${a.hash}`} className="truncate">
                    #{a.seq} {a.hash.slice(0, 16)} {a.key_id}
                  </li>
                ))}
              </ul>
            </details>
          </>
        )}
      </div>
    </section>
  );
}

/** Audit events with filters, chain check, anchors and export. */
export function AuditView({
  filters,
  events,
  nextCursor,
  paged,
  invalidFilter,
  anchors,
  canExport,
  totpEnabled,
  verify = verifyChain,
  poll = chainJob,
  download = defaultDownload,
}: AuditViewProps) {
  const t = useTranslations("Audit");
  const tErrors = useTranslations("Errors");
  const nav = useTranslations("Nav");
  const format = useFormatter();
  const locale = useLocale();
  const [quick, setQuick] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [exporting, setExporting] = useState<"csv" | "json" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const query = auditQuery(filters);

  const shown = useMemo(() => {
    const needle = quick.trim().toLowerCase();
    if (!needle) return events;
    return events.filter((e) =>
      [e.action, e.entity_type, e.entity_id, e.actor_id, e.workflow, e.request_id, String(e.seq)].some((v) =>
        (v ?? "").toLowerCase().includes(needle),
      ),
    );
  }, [events, quick]);

  const runExport = async (fmt: "csv" | "json") => {
    setExporting(fmt);
    setExportError(null);
    try {
      const result = await download(fmt, query);
      if (!result.ok) setExportError(result.code);
    } catch {
      setExportError("unavailable");
    }
    setExporting(null);
  };

  return (
    <div className="mx-auto w-full max-w-7xl space-y-8 px-5 py-10 sm:px-8">
      <PageHeader title={nav("audit")}>
        <p>{t("subtitle")}</p>
      </PageHeader>

      <ChainCard anchors={anchors} verify={verify} poll={poll} />

      <div className="space-y-4">
        <FilterForm key={query} filters={filters} />
        {invalidFilter && <FormError id="filter-error">{t("invalidFilter")}</FormError>}

        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-56 flex-1">
            <Search className="pointer-events-none absolute start-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              type="search"
              value={quick}
              onChange={(e) => setQuick(e.target.value)}
              placeholder={t("quickPlaceholder")}
              aria-label={t("quick")}
              className="h-9 ps-9"
            />
          </div>
          {canExport && (
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => runExport("csv")} disabled={exporting !== null}>
                {exporting === "csv" ? <Loader2 className="animate-spin" aria-hidden /> : <Download aria-hidden />}
                {t("exportCsv")}
              </Button>
              <Button size="sm" variant="outline" onClick={() => runExport("json")} disabled={exporting !== null}>
                {exporting === "json" ? <Loader2 className="animate-spin" aria-hidden /> : <Download aria-hidden />}
                {t("exportJson")}
              </Button>
            </div>
          )}
        </div>
        {exportError === "second_factor_required" ? (
          <StepUpPrompt totpEnabled={totpEnabled} returnPath={`/${locale}/audit`} />
        ) : (
          exportError && <FormError id="export-error">{tErrors(errorKey(exportError) as "generic")}</FormError>
        )}

        {shown.length === 0 ? (
          <EmptyState icon={<Search aria-hidden />} title={t("emptyTitle")} body={t("emptyBody")} />
        ) : (
          <div className="overflow-x-auto rounded-xl border bg-surface">
            <table className="w-full min-w-[48rem] text-sm">
              <caption className="sr-only">{t("tableCaption")}</caption>
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  <th scope="col" className="px-4 py-2.5 text-start font-medium">#</th>
                  <th scope="col" className="px-4 py-2.5 text-start font-medium">{t("columns.when")}</th>
                  <th scope="col" className="px-4 py-2.5 text-start font-medium">{t("columns.action")}</th>
                  <th scope="col" className="px-4 py-2.5 text-start font-medium">{t("columns.actor")}</th>
                  <th scope="col" className="px-4 py-2.5 text-start font-medium">{t("columns.entity")}</th>
                  <th scope="col" className="px-4 py-2.5 text-start font-medium">{t("columns.workflow")}</th>
                  <th scope="col" className="px-4 py-2.5 text-end font-medium">
                    <span className="sr-only">{t("columns.details")}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {shown.map((event) => {
                  const expanded = open === event.id;
                  return (
                    <AuditRow
                      key={event.id}
                      event={event}
                      expanded={expanded}
                      onToggle={() => setOpen(expanded ? null : event.id)}
                      when={format.dateTime(new Date(event.occurred_at), { dateStyle: "medium", timeStyle: "medium" })}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <nav aria-label={t("pagination")} className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <span className="text-muted-foreground">{t("showing", { count: shown.length })}</span>
          <span className="flex gap-2">
            {paged && (
              <Button asChild size="sm" variant="ghost">
                <Link href={`/audit${query}`}>{filters.order === "asc" ? t("first") : t("newest")}</Link>
              </Button>
            )}
            {nextCursor && (
              <Button asChild size="sm" variant="outline">
                <Link href={`/audit${auditQuery(filters, { cursor: nextCursor })}`}>
                  {filters.order === "asc" ? t("later") : t("older")}
                </Link>
              </Button>
            )}
          </span>
        </nav>
      </div>
    </div>
  );
}

function AuditRow({
  event,
  expanded,
  onToggle,
  when,
}: {
  event: AuditEvent;
  expanded: boolean;
  onToggle: () => void;
  when: string;
}) {
  const t = useTranslations("Audit");
  const detailsId = `audit-${event.id}`;
  return (
    <>
      <tr className={cn("border-b align-top last:border-b-0", expanded && "bg-accent/40")}>
        <td className="px-4 py-2.5 font-mono text-xs tabular-nums text-muted-foreground" dir="ltr">
          {event.seq}
        </td>
        <td className="px-4 py-2.5 whitespace-nowrap">{when}</td>
        <td className="px-4 py-2.5 font-mono text-xs" dir="ltr">
          {event.action}
        </td>
        <td className="px-4 py-2.5">
          <span className="block text-xs text-muted-foreground">{t(`filters.actorTypes.${knownActor(event.actor_type)}`)}</span>
          <bdi className="block max-w-48 truncate font-mono text-xs" title={event.actor_id ?? ""}>
            {event.actor_id ?? "-"}
          </bdi>
        </td>
        <td className="px-4 py-2.5">
          <span className="block text-xs text-muted-foreground">{event.entity_type ?? "-"}</span>
          <bdi className="block max-w-56 truncate font-mono text-xs" title={event.entity_id ?? ""}>
            {event.entity_id ?? ""}
          </bdi>
        </td>
        <td className="px-4 py-2.5 text-xs">{event.workflow ?? "-"}</td>
        <td className="px-4 py-2.5 text-end">
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={expanded}
            aria-controls={detailsId}
            onClick={onToggle}
            aria-label={t("toggleDetails", { seq: event.seq })}
          >
            <ChevronDown className={cn("transition-transform", expanded && "rotate-180")} aria-hidden />
          </Button>
        </td>
      </tr>
      {expanded && (
        <tr id={detailsId} className="border-b bg-surface-sunken/50">
          <td colSpan={7} className="space-y-3 px-4 py-4">
            <JsonView value={event.payload} />
            <dl className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-[10rem_1fr]">
              <dt className="text-muted-foreground">{t("hash")}</dt>
              <dd className="font-mono break-all" dir="ltr">
                {event.hash}
              </dd>
              <dt className="text-muted-foreground">{t("prevHash")}</dt>
              <dd className="font-mono break-all" dir="ltr">
                {event.prev_hash}
              </dd>
              {event.request_id && (
                <>
                  <dt className="text-muted-foreground">{t("requestId")}</dt>
                  <dd className="font-mono break-all" dir="ltr">
                    {event.request_id}
                  </dd>
                </>
              )}
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}

const knownActor = (value: string) => ACTOR_TYPES.find((a) => a === value) ?? "system";
