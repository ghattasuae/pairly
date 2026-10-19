// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Inbox as InboxIcon, Keyboard, Radio, Search } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useId, useMemo, useRef, useState, useTransition } from "react";

import { ApprovalStatusBadge, SlaBadge } from "@/components/data/status-badges";
import { GuardrailBadge } from "@/components/document/guardrail-notice";
import { useNotifications } from "@/components/notifications/notifications-provider";
import { isTypingTarget } from "@/components/shell/shortcuts";
import { EmptyState } from "@/components/states/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { Select, Switch } from "@/components/ui/form-controls";
import { useToast } from "@/components/ui/toaster";
import { useRouter } from "@/i18n/navigation";
import { decideApproval, loadApproval, loadInboxPage } from "@/lib/actions/approvals";
import { errorKey } from "@/lib/action-result";
import type { ApprovalDetail, ApprovalSummary, DecisionChoice } from "@/lib/api/types";
import {
  type InboxFilters,
  newIdempotencyKey,
  SLA_STATES,
  STATUSES,
  toQuery,
} from "@/lib/inbox-filters";
import { cn } from "@/lib/utils";

import { ApprovalPanel, decidedStatus, type DetailState, type Optimistic } from "./approval-panel";
import { DecisionDialog } from "./decision-dialog";
import { ShortcutsHelp } from "./shortcuts-help";

export interface InboxViewProps {
  filters: InboxFilters;
  initialItems: ApprovalSummary[];
  initialCursor: string | null;
  initialItem: string | null;
  initialDetail: ApprovalDetail | null;
  /** Workflows the person approves in (for the filter). */
  workflows: string[];
  meId: string;
  totpEnabled: boolean;
  /** Overrides for stories and tests. Default to the server actions. */
  actions?: {
    load: typeof loadApproval;
    decide: typeof decideApproval;
    loadPage: typeof loadInboxPage;
  };
}

interface Attempt {
  key: string;
  decision: DecisionChoice;
  reason: string;
}

const DEFAULT_ACTIONS = { load: loadApproval, decide: decideApproval, loadPage: loadInboxPage };

function anyDialogOpen(): boolean {
  return document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]') !== null;
}

/**
 * The unified approval inbox (brief 5.2.2): every approval the person may decide, across
 * workflows, in a split view. List on the start side, the open item on the end side, so
 * deciding never leaves the list (brief 5.3, "never lose your place"). Filters and the
 * open item live in the URL. Keyboard first: J/K move, Enter opens, A approves, R rejects,
 * C requests changes, / searches, ? shows help.
 *
 * Decisions are optimistic: the item shows the decision at once and the selection moves
 * on; if the API refuses, the item is restored and the reason shown. Each attempt carries
 * an Idempotency-Key, reused when the same decision is retried, so a retry after a network
 * failure can never decide twice.
 */
export function InboxView({
  filters,
  initialItems,
  initialCursor,
  initialItem,
  initialDetail,
  workflows,
  meId,
  totpEnabled,
  actions = DEFAULT_ACTIONS,
}: InboxViewProps) {
  const t = useTranslations("Inbox");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const locale = useLocale();
  const router = useRouter();
  const toast = useToast();
  const { subscribe, live } = useNotifications();
  const [, startTransition] = useTransition();
  const headingId = useId();
  const searchRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  const [items, setItems] = useState(initialItems);
  const [cursor, setCursor] = useState(initialCursor);
  const [loadingMore, setLoadingMore] = useState(false);
  const [selected, setSelected] = useState<string | null>(initialItem ?? initialItems[0]?.id ?? null);
  const [mobileDetail, setMobileDetail] = useState(initialItem !== null);
  const [details, setDetails] = useState<Record<string, DetailState>>(() =>
    initialDetail ? { [initialDetail.id]: { status: "ready", detail: initialDetail } } : {},
  );
  const [decided, setDecided] = useState<Record<string, Optimistic>>({});
  const [stepUp, setStepUp] = useState<Record<string, true>>({});
  const [dialog, setDialog] = useState<{ id: string; decision: DecisionChoice } | null>(null);
  const [reason, setReason] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [q, setQ] = useState(filters.q);
  const attempts = useRef<Record<string, Attempt>>({});
  const submitRef = useRef<(id: string, decision: DecisionChoice, text: string) => Promise<void>>(async () => {});
  const inflight = useRef(new Set<string>());

  // A refresh (live update) brings new server data: adopt it during render.
  const [source, setSource] = useState(initialItems);
  if (source !== initialItems) {
    setSource(initialItems);
    setItems(initialItems);
    setCursor(initialCursor);
  }

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) =>
      [item.title, item.approval_type, item.workflow].some((v) => v.toLowerCase().includes(needle)),
    );
  }, [items, q]);

  const summaryOf = useCallback((id: string | null) => items.find((item) => item.id === id) ?? null, [items]);

  // Keep the open item and the search in the URL (reload and back keep the place).
  useEffect(() => {
    const url = `/${locale}/inbox${toQuery({ ...filters, q }, selected)}`;
    if (`${window.location.pathname}${window.location.search}` !== url) window.history.replaceState(null, "", url);
  }, [filters, q, selected, locale]);

  const fetchDetail = useCallback(
    async (id: string, force = false) => {
      if (inflight.current.has(id)) return;
      // Loaded or failed: a failed item is retried when it is picked again (select).
      if (!force && details[id]) return;
      inflight.current.add(id);
      try {
        const result = await actions.load(id);
        setDetails((all) => ({
          ...all,
          [id]: result.ok ? { status: "ready", detail: result.detail } : { status: "error", code: result.code },
        }));
      } catch {
        setDetails((all) => ({ ...all, [id]: { status: "error", code: "unavailable" } }));
      } finally {
        inflight.current.delete(id);
      }
    },
    [actions, details],
  );

  // Load the open item, then prefetch the next one so J feels instant (brief 5.3).
  // Holding J coalesces: only the item the person stops on is fetched.
  useEffect(() => {
    if (!selected) return;
    const timers = [window.setTimeout(() => void fetchDetail(selected), 0)];
    const index = visible.findIndex((item) => item.id === selected);
    const next = index >= 0 ? visible[index + 1] : undefined;
    if (next && !details[next.id]) timers.push(window.setTimeout(() => void fetchDetail(next.id), 150));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [selected, visible, details, fetchDetail]);

  const select = useCallback((id: string, open = false) => {
    setSelected(id);
    setDetails((all) => {
      if (all[id]?.status !== "error") return all;
      const copy = { ...all };
      delete copy[id];
      return copy;
    });
    if (open) setMobileDetail(true);
    requestAnimationFrame(() => {
      document.querySelector<HTMLElement>(`[data-approval-row="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest" });
    });
  }, []);

  const move = useCallback(
    (delta: 1 | -1) => {
      if (visible.length === 0) return;
      const index = visible.findIndex((item) => item.id === selected);
      const nextIndex = index < 0 ? 0 : Math.min(visible.length - 1, Math.max(0, index + delta));
      const next = visible[nextIndex];
      if (next) select(next.id);
    },
    [visible, selected, select],
  );

  /** The item to show after deciding `id`: the next undecided one, else the previous. */
  const after = useCallback(
    (id: string): string | null => {
      const index = visible.findIndex((item) => item.id === id);
      const open = (item: ApprovalSummary) => item.id !== id && item.status === "pending" && !decided[item.id];
      return visible.slice(index + 1).find(open)?.id ?? visible.slice(0, Math.max(0, index)).reverse().find(open)?.id ?? null;
    },
    [visible, decided],
  );

  const canDecide = useCallback(
    (id: string | null) => {
      if (!id || decided[id] || stepUp[id]) return false;
      const state = details[id];
      return state?.status === "ready" && state.detail.can_decide && state.detail.status === "pending" && !state.detail.needs_second_factor;
    },
    [details, decided, stepUp],
  );

  const openDialog = useCallback(
    (decision: DecisionChoice) => {
      if (!selected || !canDecide(selected)) return;
      const previous = attempts.current[selected];
      setReason(previous && previous.decision === decision ? previous.reason : "");
      setDialog({ id: selected, decision });
    },
    [selected, canDecide],
  );

  const submit = useCallback(
    async (id: string, decision: DecisionChoice, text: string) => {
      const state = details[id];
      const stepId = state?.status === "ready" ? (state.detail.current_step?.id ?? null) : null;
      const trimmed = text.trim();
      const previous = attempts.current[id];
      const attempt =
        previous && previous.decision === decision && previous.reason === trimmed
          ? previous
          : { key: newIdempotencyKey(), decision, reason: trimmed };
      attempts.current[id] = attempt;

      // Optimistic: show the decision now, move on.
      setDecided((all) => ({ ...all, [id]: { decision, state: "sending" } }));
      const next = after(id);
      if (next) setSelected(next);

      let result: Awaited<ReturnType<typeof actions.decide>>;
      try {
        result = await actions.decide({
          approvalId: id,
          decision,
          reason: decision === "approve" ? trimmed || null : trimmed,
          idempotencyKey: attempt.key,
          stepId,
        });
      } catch {
        result = { ok: false, code: "unavailable" };
      }

      if (result.ok) {
        delete attempts.current[id];
        setDecided((all) => ({ ...all, [id]: { decision, state: "done" } }));
        setItems((all) => all.map((item) => (item.id === id ? { ...item, status: result.result.approval_status } : item)));
        setDetails((all) => {
          const copy = { ...all };
          delete copy[id];
          return copy;
        });
        const title = summaryOf(id)?.title ?? "";
        toast.show({ tone: "success", title: t(`toast.${decision}`), body: title });
        return;
      }

      // Roll back.
      setDecided((all) => {
        const copy = { ...all };
        delete copy[id];
        return copy;
      });
      if (result.code === "second_factor_required") {
        setStepUp((all) => ({ ...all, [id]: true }));
        setSelected(id);
        return;
      }
      if (result.code === "already_decided" || result.code === "approval_state_conflict") {
        void fetchDetail(id, true);
        startTransition(() => router.refresh());
      }
      toast.show({
        tone: "error",
        title: t("toast.failed"),
        body: tErrors(errorKey(result.code) as "generic"),
        action:
          result.code === "unavailable"
            ? { label: t("toast.retry"), onClick: () => void submitRef.current(id, decision, trimmed) }
            : undefined,
      });
    },
    [actions, after, details, fetchDetail, router, summaryOf, t, tErrors, toast],
  );

  // The retry button in a toast calls the latest `submit` (same Idempotency-Key).
  useEffect(() => {
    submitRef.current = submit;
  }, [submit]);

  const confirmDialog = () => {
    if (!dialog) return;
    const { id, decision } = dialog;
    setDialog(null);
    void submit(id, decision, reason);
  };

  // Keyboard shortcuts.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTypingTarget(event.target) || anyDialogOpen()) return;
      switch (event.key) {
        case "j":
        case "J":
          event.preventDefault();
          move(1);
          break;
        case "k":
        case "K":
          event.preventDefault();
          move(-1);
          break;
        case "Enter":
          if (!selected || (event.target instanceof HTMLElement && event.target.closest("button, a"))) return;
          event.preventDefault();
          setMobileDetail(true);
          requestAnimationFrame(() => document.getElementById(headingId)?.focus());
          break;
        case "a":
        case "A":
          event.preventDefault();
          openDialog("approve");
          break;
        case "r":
        case "R":
          event.preventDefault();
          openDialog("reject");
          break;
        case "c":
        case "C":
          event.preventDefault();
          openDialog("request_changes");
          break;
        case "/":
          event.preventDefault();
          searchRef.current?.focus();
          break;
        case "?":
          event.preventDefault();
          setHelpOpen(true);
          break;
        default:
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [move, openDialog, selected, headingId]);

  // Live updates: a new approval or SLA notification refreshes the list from the server.
  useEffect(() => {
    let timer: number | null = null;
    const unsubscribe = subscribe((notification) => {
      if (!["approvals", "sla"].includes(notification.category)) return;
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => startTransition(() => router.refresh()), 800);
    });
    return () => {
      unsubscribe();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [subscribe, router]);

  const changeFilter = (patch: Partial<InboxFilters>) => {
    const next = { ...filters, q, ...patch };
    startTransition(() => router.replace(`/inbox${toQuery(next, null)}`, { scroll: false }));
  };

  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const result = await actions.loadPage({ ...filters, q }, cursor);
      if (result.ok) {
        setItems((all) => [...all, ...result.items.filter((item) => !all.some((a) => a.id === item.id))]);
        setCursor(result.nextCursor);
      } else {
        toast.show({ tone: "error", title: t("loadMoreFailed") });
      }
    } catch {
      toast.show({ tone: "error", title: t("loadMoreFailed") });
    }
    setLoadingMore(false);
  };

  const current = selected ? (details[selected] ?? { status: "loading" }) : null;
  const workflowName = useTranslations("Workflows");
  const label = (id: string) => (workflowName.has(id as "pay") ? workflowName(id as "pay") : id);

  return (
    <div className="flex h-[calc(100dvh-3.5rem)] min-h-0">
      {/* List */}
      <section
        aria-labelledby="inbox-heading"
        className={cn(
          "flex w-full min-w-0 flex-col border-e border-border bg-surface lg:w-[26rem] lg:shrink-0",
          mobileDetail && "hidden lg:flex",
        )}
      >
        <div className="space-y-3 border-b border-border px-4 pt-5 pb-4">
          <div className="flex items-center justify-between gap-2">
            <h1 id="inbox-heading" className="font-display text-[1.75rem] leading-tight">
              {t("title")}
            </h1>
            <div className="flex items-center gap-1">
              <span
                className={cn("flex items-center gap-1 text-xs", live ? "text-success-ink" : "text-muted-foreground")}
                title={live ? t("live") : t("notLive")}
              >
                <Radio className="size-3.5" aria-hidden />
                <span className="sr-only">{live ? t("live") : t("notLive")}</span>
              </span>
              <Button variant="ghost" size="icon" className="size-8" aria-label={t("shortcuts.title")} onClick={() => setHelpOpen(true)}>
                <Keyboard aria-hidden />
              </Button>
            </div>
          </div>
          <div className="relative">
            <Search className="pointer-events-none absolute start-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              ref={searchRef}
              type="search"
              value={q}
              onChange={(event) => setQ(event.target.value.slice(0, 100))}
              onKeyDown={(event) => {
                if (event.key === "Escape") (event.target as HTMLInputElement).blur();
              }}
              placeholder={t("searchPlaceholder")}
              aria-label={t("search")}
              aria-keyshortcuts="/"
              className="ps-9 pe-10"
            />
            <Kbd className="pointer-events-none absolute end-3 top-1/2 -translate-y-1/2">/</Kbd>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Select
              aria-label={t("filters.status")}
              value={filters.status}
              onChange={(event) => changeFilter({ status: event.target.value as InboxFilters["status"] })}
              className="h-9"
            >
              {STATUSES.map((status) => (
                <option key={status} value={status}>
                  {t(`filters.statuses.${status}`)}
                </option>
              ))}
            </Select>
            <Select
              aria-label={t("filters.sla")}
              value={filters.sla ?? ""}
              onChange={(event) => changeFilter({ sla: (event.target.value || null) as InboxFilters["sla"] })}
              className="h-9"
            >
              <option value="">{t("filters.anySla")}</option>
              {SLA_STATES.map((sla) => (
                <option key={sla} value={sla}>
                  {t(`filters.slas.${sla}`)}
                </option>
              ))}
            </Select>
            {workflows.length > 1 && (
              <Select
                aria-label={t("filters.workflow")}
                value={filters.workflow ?? ""}
                onChange={(event) => changeFilter({ workflow: event.target.value || null })}
                className="h-9"
              >
                <option value="">{t("filters.allWorkflows")}</option>
                {workflows.map((w) => (
                  <option key={w} value={w}>
                    {label(w)}
                  </option>
                ))}
              </Select>
            )}
            <label className="flex h-9 items-center gap-2 text-sm">
              <Switch checked={filters.mine} onCheckedChange={(mine) => changeFilter({ mine })} aria-label={t("filters.mine")} />
              <span aria-hidden>{t("filters.mine")}</span>
            </label>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {visible.length === 0 ? (
            <EmptyState
              className="px-5"
              icon={<InboxIcon aria-hidden />}
              title={items.length === 0 ? t("emptyTitle") : t("noMatchTitle")}
              body={items.length === 0 ? t("emptyBody") : t("noMatchBody")}
            />
          ) : (
            <ul ref={listRef} aria-label={t("listLabel")} className="divide-y divide-border">
              {visible.map((item) => {
                const optimistic = decided[item.id];
                const status = optimistic?.state === "done" ? decidedStatus(optimistic.decision) : item.status;
                const active = item.id === selected;
                return (
                  <li key={item.id}>
                    <a
                      href={`/${locale}/inbox${toQuery({ ...filters, q }, item.id)}`}
                      data-approval-row={item.id}
                      aria-current={active ? "true" : undefined}
                      onClick={(event) => {
                        if (event.metaKey || event.ctrlKey || event.shiftKey) return;
                        event.preventDefault();
                        select(item.id, true);
                      }}
                      className={cn(
                        // The focus ring sits inside the row so the scrolling list never clips it.
                        "relative block space-y-1.5 py-3 ps-5 pe-4 transition-colors hover:bg-accent/60 focus-visible:-outline-offset-2",
                        active && "bg-accent",
                        optimistic && "opacity-80",
                      )}
                    >
                      {active && <span aria-hidden className="absolute inset-y-0 start-0 w-[3px] bg-signal" />}
                      <span className="flex items-start justify-between gap-2">
                        <span className={cn("line-clamp-2 text-sm font-medium", optimistic && "line-through decoration-muted-foreground/60")}>
                          <bdi>{item.title}</bdi>
                        </span>
                        <time dateTime={item.created_at} className="shrink-0 pt-0.5 text-xs text-muted-foreground">
                          {format.relativeTime(new Date(item.created_at), new Date())}
                        </time>
                      </span>
                      <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                        <span>{label(item.workflow)}</span>
                        {item.current_step && (
                          <span>
                            · <bdi>{item.current_step.name}</bdi>
                          </span>
                        )}
                        {status !== "pending" || optimistic ? (
                          <ApprovalStatusBadge status={optimistic?.state === "sending" ? "pending" : status} />
                        ) : (
                          <SlaBadge state={item.sla_state} />
                        )}
                        <GuardrailBadge count={item.guardrail_findings} />
                      </span>
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
          {cursor && (
            <div className="p-4">
              <Button variant="outline" className="w-full" onClick={loadMore} disabled={loadingMore}>
                {loadingMore ? t("loadingMore") : t("loadMore")}
              </Button>
            </div>
          )}
        </div>
        <p className="hidden border-t border-border bg-surface-sunken px-4 py-2 text-xs text-muted-foreground lg:block">
          {t.rich("hint", { kbd: (chunks) => <Kbd>{chunks}</Kbd> })}
        </p>
      </section>

      {/* Detail */}
      <section
        aria-label={t("detailLabel")}
        className={cn("min-w-0 flex-1 overflow-y-auto bg-background", !mobileDetail && "hidden lg:block")}
      >
        {/* On a phone the list (and its heading) is hidden while an item is open. */}
        {mobileDetail && <h1 className="sr-only lg:hidden">{t("title")}</h1>}
        {selected && current ? (
          <ApprovalPanel
            key={selected}
            summary={summaryOf(selected)}
            state={current}
            meId={meId}
            optimistic={decided[selected] ?? null}
            stepUp={!!stepUp[selected]}
            totpEnabled={totpEnabled}
            returnPath={`/${locale}/inbox/${selected}`}
            onDecide={openDialog}
            onBack={() => setMobileDetail(false)}
            headingId={headingId}
          />
        ) : (
          <div className="flex h-full items-center justify-center p-8">
            <p className="max-w-xs text-center font-display text-xl leading-snug text-muted-foreground">{t("pickOne")}</p>
          </div>
        )}
      </section>

      <DecisionDialog
        decision={dialog?.decision ?? null}
        title={summaryOf(dialog?.id ?? null)?.title ?? ""}
        reason={reason}
        onReasonChange={setReason}
        onConfirm={confirmDialog}
        onClose={() => setDialog(null)}
      />
      <ShortcutsHelp open={helpOpen} onOpenChange={setHelpOpen} />
    </div>
  );
}
