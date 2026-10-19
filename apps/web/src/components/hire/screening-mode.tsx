// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import {
  ArrowRight,
  CalendarClock,
  ChevronDown,
  ChevronUp,
  Columns3,
  EyeOff,
  Eye,
  Hand,
  ListChecks,
  Loader2,
  MessageSquareText,
  PanelLeft,
  Search,
  Tag,
  UserRoundX,
} from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type * as React from "react";

import { isTypingTarget } from "@/components/shell/shortcuts";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Checkbox, Select, Switch, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { useToast } from "@/components/ui/toaster";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { errorKey } from "@/lib/action-result";
import * as hireActions from "@/lib/actions/hire";
import * as screeningActions from "@/lib/actions/screening";
import { actionKey } from "@/lib/hire/board";
import {
  type CandidateDetail,
  DetailCache,
  filterQueue,
  HOLD_TAG,
  neighbour,
  positionOf,
  prefetchIds,
  type QueueItem,
  type ScreeningQueue,
  screenQuery,
  type ScreenState,
  withTag,
} from "@/lib/hire/screening";
import type { HireBulkSummary, HireMessage } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

import { CompareView } from "./compare-view";
import { ComposePanel } from "./compose-panel";
import { schedulePanel, ScheduleSlot } from "./schedule-slot";
import "./scheduling/screening-schedule-panel";
import { ScorePanel } from "./score-panel";
import { ScreeningDocument } from "./screening-document";

export type ScreenActions = typeof hireActions & typeof screeningActions;

export interface ScreeningModeProps {
  queue: ScreeningQueue;
  initialState: ScreenState;
  initialDetail: CandidateDetail | null;
  canMove: boolean;
  canNote: boolean;
  canMessage: boolean;
  canOverride: boolean;
  totpEnabled: boolean;
  /** Overrides for stories and tests. Default to the server actions. */
  actions?: Partial<ScreenActions>;
}

type Panel =
  | { mode: "none" }
  | { mode: "note" }
  | { mode: "tag" }
  | { mode: "reject"; message: HireMessage }
  | { mode: "schedule" };

const UNDO_MS = 10_000;

function anyDialogOpen(): boolean {
  return document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]') !== null;
}

/**
 * Recruiter screening mode (brief 5.3, the most important screen): the CV on the left,
 * the rubric scores with their evidence on the right, the queue beside them, never a new
 * page.
 *
 * - Keyboard first: J/K next and previous, A advance, H hold, R draft a rejection (it
 *   goes to the approval queue, never sent at once), N note, S schedule, T tag,
 *   X select, C compare, / search, Esc closes a panel. Every key is also a button.
 * - The next candidates load in the background (prefetch), so J shows them at once.
 * - The position never jumps: actions keep the current candidate, and the queue order
 *   is fixed (oldest application first).
 * - Reversible actions (advance, hold, tag, a move) show a 10 second undo toast.
 * - Bulk: select with X, move many with a summary first; compare two or three.
 * - Blind mode is always visible with what it hides; admins can change it here.
 */
export function ScreeningMode({
  queue: initialQueue,
  initialState,
  initialDetail,
  canMove,
  canNote,
  canMessage,
  canOverride,
  totpEnabled,
  actions: overrides,
}: ScreeningModeProps) {
  const t = useTranslations("Screen");
  const tHire = useTranslations("Hire");
  const tErrors = useTranslations("Errors");
  const locale = useLocale();
  const toast = useToast();
  const actions = useMemo(() => ({ ...hireActions, ...screeningActions, ...overrides }), [overrides]);
  const jobId = initialQueue.job_id;

  const [queue, setQueue] = useState(initialQueue);
  const [q, setQ] = useState(initialState.q);
  const [current, setCurrent] = useState<string | null>(
    initialState.candidate ?? initialQueue.items[0]?.application_id ?? null,
  );
  // One cache for the life of the screen (state, not a ref: read while rendering).
  const [cache] = useState(() => {
    const made = new DetailCache();
    const first = initialState.candidate ?? initialQueue.items[0]?.application_id ?? null;
    if (initialDetail && first) made.set(first, initialDetail);
    return made;
  });
  const [detail, setDetail] = useState<CandidateDetail | null>(initialDetail);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState<{ id: string; seq: number } | null>(null);
  const [selection, setSelection] = useState<string[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);
  const [panel, setPanel] = useState<Panel>({ mode: "none" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queueOpen, setQueueOpen] = useState(false);
  const [bulkStage, setBulkStage] = useState("");
  const [bulkSummary, setBulkSummary] = useState<HireBulkSummary | null>(null);
  const [noteText, setNoteText] = useState("");
  const [tagText, setTagText] = useState("");
  const searchRef = useRef<HTMLInputElement | null>(null);
  const noteRef = useRef<HTMLTextAreaElement | null>(null);
  const tagRef = useRef<HTMLInputElement | null>(null);
  const loadingId = useRef<string | null>(null);

  const visible = useMemo(() => filterQueue(queue.items, q), [queue.items, q]);
  const item = useMemo(() => queue.items.find((i) => i.application_id === current) ?? null, [queue.items, current]);
  const stageLabel = useCallback(
    (key: string) => {
      const stage = queue.stages.find((s) => s.key === key);
      return stage ? (locale === "ar" && stage.label_ar ? stage.label_ar : stage.label) : key;
    },
    [queue.stages, locale],
  );
  const scheduling = queue.scheduling_available && schedulePanel() !== null;

  // ------------------------------------------------------------ URL

  useEffect(() => {
    const url = `/${locale}/hire/jobs/${jobId}/screen${screenQuery({ candidate: current, q })}`;
    if (`${window.location.pathname}${window.location.search}` !== url) window.history.replaceState(null, "", url);
  }, [current, q, locale, jobId]);

  // ------------------------------------------------------------ data

  const fetchDetail = useCallback(
    async (id: string): Promise<CandidateDetail | null> => {
      const result = await actions.loadCandidate(id);
      if (!result.ok) return null;
      cache.set(id, result.detail);
      return result.detail;
    },
    [actions, cache],
  );

  const prefetch = useCallback(
    (id: string | null) => {
      for (const next of prefetchIds(visible, id)) {
        if (!cache.has(next)) void fetchDetail(next);
      }
    },
    [cache, fetchDetail, visible],
  );

  const show = useCallback(
    async (id: string | null, { fresh = false }: { fresh?: boolean } = {}) => {
      setCurrent(id);
      setActive(null);
      setPanel({ mode: "none" });
      setError(null);
      if (!id) {
        setDetail(null);
        return;
      }
      if (fresh) cache.delete(id);
      const cached = cache.get(id);
      if (cached) {
        setDetail(cached);
        setLoading(false);
      } else {
        setLoading(true);
        loadingId.current = id;
        const loaded = await fetchDetail(id);
        if (loadingId.current !== id) return;
        setLoading(false);
        if (loaded) setDetail(loaded);
        else setError("unavailable");
      }
    },
    [cache, fetchDetail],
  );

  // Prefetch around the current candidate whenever it changes.
  useEffect(() => {
    prefetch(current);
  }, [current, prefetch]);

  const refreshQueue = useCallback(async () => {
    const result = await actions.loadQueue(jobId);
    if (result.ok) setQueue(result.queue);
  }, [actions, jobId]);

  /** Reload the current candidate after an action, staying exactly where we are. */
  const reloadCurrent = useCallback(
    async (id: string) => {
      cache.delete(id);
      const loaded = await fetchDetail(id);
      if (loaded) setDetail((d) => (d && d.profile.application_id === id ? loaded : d));
    },
    [cache, fetchDetail],
  );

  const patchItem = (id: string, patch: Partial<QueueItem>) =>
    setQueue((qq) => ({ ...qq, items: qq.items.map((i) => (i.application_id === id ? { ...i, ...patch } : i)) }));

  const fail = (code: string) => {
    setError(code);
    if (code !== "second_factor_required") {
      toast.show({ tone: "error", title: tErrors(errorKey(code) as "generic") });
    }
  };

  // ------------------------------------------------------------ actions

  const undoMoves = useCallback(
    async (moveIds: string[], id: string) => {
      const result = await actions.undoMoves(moveIds);
      if (!result.ok) {
        toast.show({ tone: "error", title: t("toast.undoFailed") });
        return;
      }
      toast.show({ tone: "info", title: t("toast.undone"), duration: 3000 });
      await Promise.all([refreshQueue(), reloadCurrent(id)]);
    },
    [actions, refreshQueue, reloadCurrent, t, toast],
  );

  const advance = useCallback(async () => {
    if (!item || !canMove || busy) return;
    const id = item.application_id;
    setBusy(true);
    setError(null);
    const result = await actions.advanceCandidate({ applicationId: id, idempotencyKey: actionKey() });
    setBusy(false);
    if (!result.ok) return fail(result.code);
    patchItem(id, { stage_key: result.move.to_stage });
    toast.show({
      tone: "success",
      title: t("toast.moved", { name: item.label, stage: stageLabel(result.move.to_stage) }),
      duration: UNDO_MS,
      action: { label: t("toast.undo"), onClick: () => void undoMoves([result.move.move_id], id) },
    });
    void refreshQueue();
    void reloadCurrent(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actions, busy, canMove, item, refreshQueue, reloadCurrent, stageLabel, t, toast, undoMoves]);

  const changeTags = useCallback(
    async (next: string[], toastTitle: string) => {
      if (!item || !detail || !canNote || busy) return false;
      const id = item.application_id;
      const previous = [...detail.profile.tags];
      setBusy(true);
      const result = await actions.setTags(id, next);
      setBusy(false);
      if (!result.ok) {
        fail(result.code);
        return false;
      }
      patchItem(id, { tags: result.tags });
      setDetail((d) => (d && d.profile.application_id === id ? { ...d, profile: { ...d.profile, tags: result.tags } } : d));
      cache.delete(id);
      toast.show({
        tone: "success",
        title: toastTitle,
        duration: UNDO_MS,
        action: {
          label: t("toast.undo"),
          onClick: () =>
            void (async () => {
              const back = await actions.setTags(id, previous);
              if (back.ok) {
                patchItem(id, { tags: back.tags });
                toast.show({ tone: "info", title: t("toast.undone"), duration: 3000 });
                await reloadCurrent(id);
              } else toast.show({ tone: "error", title: t("toast.undoFailed") });
            })(),
        },
      });
      return true;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [actions, busy, canNote, detail, item, reloadCurrent, t, toast],
  );

  const hold = useCallback(() => {
    if (!detail || !item) return;
    void changeTags(withTag(detail.profile.tags, HOLD_TAG), t("toast.held", { name: item.label }));
  }, [changeTags, detail, item, t]);

  const reject = useCallback(async () => {
    if (!item || !canMove || busy) return;
    const id = item.application_id;
    if (item.stage_key === "rejected") {
      if (item.open_message_id) {
        const message = await actions.loadMessage(item.open_message_id);
        if (message.ok) setPanel({ mode: "reject", message: message.message });
      }
      return;
    }
    setBusy(true);
    setError(null);
    const result = await actions.moveCandidate({ applicationId: id, toStage: "rejected", idempotencyKey: actionKey() });
    if (!result.ok) {
      setBusy(false);
      return fail(result.code);
    }
    patchItem(id, { stage_key: "rejected" });
    toast.show({
      tone: "success",
      title: t("toast.rejectDraft", { name: item.label }),
      duration: UNDO_MS,
      action: { label: t("toast.undo"), onClick: () => void undoMoves([result.move.move_id], id) },
    });
    if (result.move.draft_id) {
      const message = await actions.loadMessage(result.move.draft_id);
      if (message.ok) setPanel({ mode: "reject", message: message.message });
    }
    setBusy(false);
    void refreshQueue();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actions, busy, canMove, item, refreshQueue, t, toast, undoMoves]);

  const saveNote = async () => {
    if (!item || !noteText.trim()) return;
    setBusy(true);
    const result = await actions.addNote({ applicationId: item.application_id, body: noteText.trim(), mentions: [] });
    setBusy(false);
    if (!result.ok) return fail(result.code);
    setNoteText("");
    setPanel({ mode: "none" });
    toast.show({ tone: "success", title: t("toast.noted") });
    void reloadCurrent(item.application_id);
  };

  const saveTag = async () => {
    if (!detail || !item || !tagText.trim()) return;
    if (await changeTags(withTag(detail.profile.tags, tagText.trim()), t("toast.tagged", { name: item.label }))) {
      setTagText("");
      setPanel({ mode: "none" });
    }
  };

  const override = async (criterionId: string, score: number, reason: string) => {
    if (!item) return false;
    const result = await actions.overrideScores({ applicationId: item.application_id, scores: { [criterionId]: score }, reason });
    if (!result.ok) {
      fail(result.code);
      return false;
    }
    const id = item.application_id;
    setDetail((d) => (d && d.profile.application_id === id ? { ...d, screening: result.screening } : d));
    const cached = cache.get(id);
    if (cached) cache.set(id, { ...cached, screening: result.screening });
    patchItem(id, { overall_score: result.screening.overall_score ?? null });
    toast.show({ tone: "success", title: t("override.saved") });
    return true;
  };

  const toggleBlind = async (enabled: boolean) => {
    const result = await actions.setBlindMode(enabled);
    if (!result.ok) return fail(result.code);
    toast.show({ tone: "info", title: t("blind.changed") });
    await refreshQueue();
  };

  const toggleSelect = (id: string) =>
    setSelection((all) => (all.includes(id) ? all.filter((x) => x !== id) : [...all, id]));

  const openCompare = () => {
    const chosen = selection.slice(-3);
    if (chosen.length < 2) {
      toast.show({ tone: "info", title: t("compare.need") });
      return;
    }
    for (const id of chosen) if (!cache.has(id)) void fetchDetail(id).then(() => setSelection((s) => [...s]));
    setCompareOpen(true);
  };

  const bulkPreview = async () => {
    if (!bulkStage || selection.length === 0) return;
    const result = await actions.bulkPreview({ jobId, applicationIds: selection, toStage: bulkStage });
    if (!result.ok) return fail(result.code);
    setBulkSummary(result.summary);
  };

  const bulkRun = async () => {
    if (!bulkSummary) return;
    setBusy(true);
    const result = await actions.bulkMove({
      jobId,
      applicationIds: selection,
      toStage: bulkSummary.to_stage,
      idempotencyKey: actionKey(),
    });
    setBusy(false);
    setBulkSummary(null);
    if (!result.ok) return fail(result.code);
    const moved = result.result.moves;
    setSelection([]);
    toast.show({
      tone: "success",
      title: tHire("bulkMoved", { count: moved.length, stage: stageLabel(bulkSummary.to_stage) }),
      duration: UNDO_MS,
      action: {
        label: t("toast.undo"),
        onClick: () => void undoMoves(moved.map((m) => m.move_id), current ?? ""),
      },
    });
    await refreshQueue();
    if (current) await reloadCurrent(current);
  };

  // ------------------------------------------------------------ keyboard

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target) || anyDialogOpen()) return;
      const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
      const handled = () => event.preventDefault();
      if (key === "j" || key === "ArrowDown") {
        handled();
        void show(neighbour(visible, current, 1));
      } else if (key === "k" || key === "ArrowUp") {
        handled();
        void show(neighbour(visible, current, -1));
      } else if (key === "a") {
        handled();
        void advance();
      } else if (key === "h") {
        handled();
        hold();
      } else if (key === "r") {
        handled();
        void reject();
      } else if (key === "n" && canNote) {
        handled();
        setPanel({ mode: "note" });
      } else if (key === "t" && canNote) {
        handled();
        setPanel({ mode: "tag" });
      } else if (key === "s") {
        handled();
        if (scheduling) setPanel({ mode: "schedule" });
      } else if (key === "x" && current) {
        handled();
        toggleSelect(current);
      } else if (key === "c") {
        handled();
        openCompare();
      } else if (key === "/") {
        handled();
        searchRef.current?.focus();
      } else if (key === "Escape" && panel.mode !== "none") {
        handled();
        setPanel({ mode: "none" });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [advance, canNote, current, hold, panel.mode, reject, scheduling, show, visible, selection]);

  useEffect(() => {
    if (panel.mode === "note") noteRef.current?.focus();
    if (panel.mode === "tag") tagRef.current?.focus();
  }, [panel.mode]);

  // ------------------------------------------------------------ render

  const position = positionOf(visible, current);
  const profile = detail && detail.profile.application_id === current ? detail.profile : null;
  const screening = detail && detail.profile.application_id === current ? detail.screening : null;
  const returnPath = `/${locale}/hire/jobs/${jobId}/screen`;
  const compareCandidates = selection.slice(-3).map((id) => ({
    id,
    label: queue.items.find((i) => i.application_id === id)?.label ?? id,
    detail: cache.peek(id),
  }));

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex min-h-0 flex-1 flex-col" data-screening-mode>
        <header className="space-y-3 border-b border-border px-4 pt-5 pb-3 sm:px-6">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <p className="eyebrow">{t("eyebrow")}</p>
              <h1 className="font-display text-[1.75rem] leading-[1.1] text-balance sm:text-[2.2rem]">{queue.job_title}</h1>
            </div>
            <BlindStatus
              enabled={queue.blind_mode}
              universities={queue.blind_universities}
              canChange={queue.can_change_blind_mode}
              revealed={profile ? !profile.identity_hidden : null}
              onToggle={(v) => void toggleBlind(v)}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="lg:hidden"
              aria-expanded={queueOpen}
              aria-controls="screen-queue"
              onClick={() => setQueueOpen((v) => !v)}
            >
              <PanelLeft aria-hidden />
              {queueOpen ? t("hideQueue") : t("showQueue")}
            </Button>
            <div className="relative min-w-48 flex-1 sm:max-w-xs" role="search">
              <Search className="pointer-events-none absolute start-2.5 top-2.5 size-4 text-muted-foreground" aria-hidden />
              <Input
                ref={searchRef}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape" || e.key === "Enter") (e.target as HTMLInputElement).blur();
                }}
                placeholder={t("search")}
                aria-label={t("search")}
                aria-keyshortcuts="/"
                className="h-9 ps-8"
              />
            </div>
            <span className="text-sm text-muted-foreground tabular-nums" data-position aria-live="polite">
              {position > 0 ? t("position", { position, total: visible.length }) : null}
            </span>
            <div className="ms-auto flex items-center gap-1">
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={t("previous")}
                title={t("previous")}
                onClick={() => void show(neighbour(visible, current, -1))}
              >
                <ChevronUp aria-hidden />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={t("next")}
                title={t("next")}
                onClick={() => void show(neighbour(visible, current, 1))}
              >
                <ChevronDown aria-hidden />
              </Button>
            </div>
          </div>
          {selection.length > 0 && (
            <div
              className="flex flex-wrap items-center gap-2 rounded-lg border border-foreground/20 bg-surface px-3 py-2 text-sm"
              role="region"
              aria-label={tHire("bulk.title")}
              data-bulk-bar
            >
              <span className="font-medium">{t("bulk.selected", { count: selection.length })}</span>
              <Select
                aria-label={t("bulk.moveTo")}
                value={bulkStage}
                onChange={(e) => setBulkStage(e.target.value)}
                className="h-8 w-auto text-sm"
              >
                <option value="">{t("bulk.moveTo")}</option>
                {queue.stages.map((s) => (
                  <option key={s.key} value={s.key}>
                    {stageLabel(s.key)}
                  </option>
                ))}
              </Select>
              <Button type="button" size="sm" variant="ink" disabled={!bulkStage || busy || !canMove} onClick={() => void bulkPreview()}>
                {t("bulk.apply")}
              </Button>
              <Button type="button" size="sm" variant="outline" onClick={openCompare} disabled={selection.length < 2}>
                <Columns3 aria-hidden />
                {t("compare.open", { count: Math.min(selection.length, 3) })}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setSelection([])}>
                {t("bulk.clear")}
              </Button>
            </div>
          )}
          {error === "second_factor_required" && (
            <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} compact />
          )}
        </header>

        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[minmax(0,1fr)_22rem] lg:grid-cols-[15rem_minmax(0,1fr)_26rem]">
          <nav
            id="screen-queue"
            aria-label={t("queue")}
            className={cn(
              "min-h-0 overflow-y-auto border-e border-border bg-surface-sunken",
              queueOpen ? "block md:col-span-2 lg:col-span-1" : "hidden lg:block",
            )}
          >
            {visible.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">{queue.items.length === 0 ? t("empty") : t("noMatch")}</p>
            ) : (
              <ol className="divide-y divide-border">
                {visible.map((entry) => (
                  <QueueRow
                    key={entry.application_id}
                    entry={entry}
                    current={entry.application_id === current}
                    selected={selection.includes(entry.application_id)}
                    stageLabel={stageLabel}
                    onOpen={() => {
                      setQueueOpen(false);
                      void show(entry.application_id);
                    }}
                    onToggle={() => toggleSelect(entry.application_id)}
                  />
                ))}
              </ol>
            )}
          </nav>

          <div className="min-h-0 overflow-y-auto p-4 sm:p-5" data-screen-left>
            {profile && (
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <h2 className="font-display text-xl" data-candidate-label>
                  {profile.label}
                </h2>
                <span className="font-mono text-xs text-muted-foreground" dir="ltr">
                  {profile.reference}
                </span>
                <Badge variant="outline">{t("stage", { stage: stageLabel(profile.stage_key) })}</Badge>
                {profile.tags.map((tag) => (
                  <Badge key={tag} variant={tag === HOLD_TAG ? "warning" : "default"}>
                    {tag}
                  </Badge>
                ))}
                {profile.needs_human_review && <Badge variant="signal">{t("needsPerson")}</Badge>}
              </div>
            )}
            {loading && !profile ? (
              <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
                <Loader2 className="size-4 animate-spin" aria-hidden />
                {t("loading")}
              </p>
            ) : (
              <ScreeningDocument screening={screening} active={active} />
            )}
          </div>

          <aside
            aria-label={t("scores")}
            className="min-h-0 overflow-y-auto border-s border-border bg-surface p-4 sm:p-5"
            data-screen-right
            data-candidate={current ?? ""}
          >
            {panel.mode === "reject" ? (
              <div className="space-y-3">
                <h2 className="font-medium">{t("reject.title")}</h2>
                <p className="text-sm text-muted-foreground">{t("reject.help")}</p>
                <ComposePanel
                  key={panel.message.id}
                  message={panel.message}
                  onBack={() => setPanel({ mode: "none" })}
                  onDone={() => {
                    setPanel({ mode: "none" });
                    void refreshQueue();
                    if (current) void reloadCurrent(current);
                  }}
                  onError={(code) => fail(code)}
                  actions={{
                    edit: actions.editMessage,
                    ai: actions.aiDraftMessage,
                    send: actions.sendMessage,
                    submit: actions.submitMessage,
                    cancel: actions.cancelMessage,
                  }}
                />
              </div>
            ) : panel.mode === "schedule" && scheduling && current && profile ? (
              <ScheduleSlot
                applicationId={current}
                label={profile.label}
                onClose={() => setPanel({ mode: "none" })}
                onDone={() => {
                  setPanel({ mode: "none" });
                  void reloadCurrent(current);
                }}
              />
            ) : (
              <div className="space-y-4">
                {panel.mode === "note" && (
                  <form
                    className="card-hairline space-y-2 bg-surface-sunken p-3"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void saveNote();
                    }}
                    data-note-form
                  >
                    <label className="block space-y-1 text-sm">
                      <span className="font-medium">{t("note.title")}</span>
                      <Textarea
                        ref={noteRef}
                        value={noteText}
                        onChange={(e) => setNoteText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                            e.preventDefault();
                            void saveNote();
                          }
                          if (e.key === "Escape") setPanel({ mode: "none" });
                        }}
                        placeholder={t("note.placeholder")}
                        rows={3}
                        maxLength={4000}
                      />
                    </label>
                    <div className="flex gap-2">
                      <Button type="submit" size="sm" variant="ink" disabled={busy || !noteText.trim()}>
                        {t("note.save")}
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => setPanel({ mode: "none" })}>
                        {t("note.cancel")}
                      </Button>
                    </div>
                  </form>
                )}
                {panel.mode === "tag" && (
                  <form
                    className="card-hairline space-y-2 bg-surface-sunken p-3"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void saveTag();
                    }}
                    data-tag-form
                  >
                    <label className="block space-y-1 text-sm">
                      <span className="font-medium">{t("tag.title")}</span>
                      <Input
                        ref={tagRef}
                        value={tagText}
                        onChange={(e) => setTagText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") setPanel({ mode: "none" });
                        }}
                        placeholder={t("tag.placeholder")}
                        maxLength={32}
                      />
                    </label>
                    <div className="flex gap-2">
                      <Button type="submit" size="sm" variant="ink" disabled={busy || !tagText.trim()}>
                        {t("tag.save")}
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => setPanel({ mode: "none" })}>
                        {t("tag.cancel")}
                      </Button>
                    </div>
                  </form>
                )}
                {screening ? (
                  <ScorePanel
                    screening={screening}
                    activeEvidence={active?.id ?? null}
                    canOverride={canOverride}
                    onQuote={(id) => setActive((a) => ({ id, seq: (a?.seq ?? 0) + 1 }))}
                    onOverride={override}
                  />
                ) : profile ? (
                  <p className="text-sm text-muted-foreground">{t("notScreened")}</p>
                ) : null}
              </div>
            )}
          </aside>
        </div>

        <ActionBar
          disabled={!current || busy}
          canMove={canMove}
          canNote={canNote}
          canMessage={canMessage}
          scheduling={scheduling}
          selected={!!current && selection.includes(current)}
          onAdvance={() => void advance()}
          onHold={hold}
          onReject={() => void reject()}
          onNote={() => setPanel({ mode: "note" })}
          onSchedule={() => scheduling && setPanel({ mode: "schedule" })}
          onTag={() => setPanel({ mode: "tag" })}
          onSelect={() => current && toggleSelect(current)}
          onCompare={openCompare}
        />

        <CompareView open={compareOpen} onOpenChange={setCompareOpen} candidates={compareCandidates} />
        <ConfirmDialog
          open={bulkSummary !== null}
          onOpenChange={(open) => !open && setBulkSummary(null)}
          title={t("bulk.summaryTitle")}
          confirmLabel={t("bulk.confirm")}
          pending={busy}
          onConfirm={() => void bulkRun()}
        >
          {bulkSummary && (
            <div className="space-y-1 text-sm" data-bulk-summary>
              <p>{tHire("bulk.summaryMove", { count: bulkSummary.will_move, total: bulkSummary.total })}</p>
              {bulkSummary.already_there.length > 0 && (
                <p>{tHire("bulk.summaryAlready", { count: bulkSummary.already_there.length })}</p>
              )}
              {bulkSummary.drafts_to_create > 0 && (
                <p>{tHire("bulk.summaryDrafts", { count: bulkSummary.drafts_to_create })}</p>
              )}
              <p className="font-medium">{tHire("bulk.summaryNothingSent")}</p>
            </div>
          )}
        </ConfirmDialog>
      </div>
    </TooltipProvider>
  );
}

function BlindStatus({
  enabled,
  universities,
  canChange,
  revealed,
  onToggle,
}: {
  enabled: boolean;
  universities: boolean;
  canChange: boolean;
  revealed: boolean | null;
  onToggle: (enabled: boolean) => void;
}) {
  const t = useTranslations("Screen");
  return (
    <section
      aria-label={enabled ? t("blind.on") : t("blind.off")}
      className="card-hairline max-w-xl space-y-1 px-3 py-2 text-sm"
      data-blind-status={enabled ? "on" : "off"}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={enabled ? "ink" : "outline"}>
          {enabled ? <EyeOff className="size-3" aria-hidden /> : <Eye className="size-3" aria-hidden />}
          {enabled ? t("blind.on") : t("blind.off")}
        </Badge>
        {revealed !== null && (
          <span className="text-xs text-muted-foreground" data-identity={revealed ? "shown" : "hidden"}>
            {revealed ? t("blind.revealed") : t("blind.hidden")}
          </span>
        )}
        {canChange && (
          <label className="ms-auto flex items-center gap-2 text-xs">
            <Switch checked={enabled} onCheckedChange={(v) => onToggle(v === true)} aria-describedby="blind-toggle-help" />
            {t("blind.toggle")}
          </label>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {enabled ? t("blind.explain", { universities: universities ? "true" : "false" }) : t("blind.explainOff")}
      </p>
      {canChange && (
        <p id="blind-toggle-help" className="text-xs text-muted-foreground">
          {t("blind.toggleHelp")}
        </p>
      )}
    </section>
  );
}

function QueueRow({
  entry,
  current,
  selected,
  stageLabel,
  onOpen,
  onToggle,
}: {
  entry: QueueItem;
  current: boolean;
  selected: boolean;
  stageLabel: (key: string) => string;
  onOpen: () => void;
  onToggle: () => void;
}) {
  const t = useTranslations("Screen");
  return (
    <li
      className={cn("relative flex items-start gap-2 px-3 py-2.5 hover:bg-accent/60", current && "bg-accent")}
      data-queue-item={entry.application_id}
      data-stage={entry.stage_key}
      data-screening-status={entry.screening_status ?? "none"}
      aria-current={current ? "true" : undefined}
    >
      {current && <span aria-hidden className="absolute inset-y-0 start-0 w-[3px] bg-signal" />}
      <Checkbox
        checked={selected}
        onChange={onToggle}
        aria-label={`${t("actions.select")} ${entry.label}`}
        className="mt-1"
        tabIndex={-1}
      />
      <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-start">
        <span className="block truncate text-sm font-medium">{entry.label}</span>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>{stageLabel(entry.stage_key)}</span>
          {entry.overall_score !== null && entry.overall_score !== undefined && (
            <span className="font-mono">{Math.round(entry.overall_score)}</span>
          )}
          {entry.tags.includes(HOLD_TAG) && <Hand className="size-3" aria-label={HOLD_TAG} />}
        </span>
      </button>
    </li>
  );
}

function ActionButton({
  label,
  keyName,
  icon,
  onClick,
  disabled,
  hint,
  data,
  primary = false,
}: {
  label: string;
  keyName: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled: boolean;
  hint?: string;
  data: string;
  /** The one saffron action of the view (advance); the rest stay outline. */
  primary?: boolean;
}) {
  const button = (
    <Button
      type="button"
      variant={primary ? "default" : "outline"}
      size="sm"
      onClick={onClick}
      disabled={disabled}
      aria-keyshortcuts={keyName}
      aria-describedby={hint ? `${data}-hint` : undefined}
      data-action={data}
    >
      {icon}
      {label}
      <Kbd
        className={cn(
          "ms-1 hidden sm:inline-flex",
          primary && "border-action-foreground/25 bg-transparent text-action-foreground",
        )}
      >
        {keyName.toUpperCase()}
      </Kbd>
    </Button>
  );
  if (!hint) return button;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* A disabled button gets no pointer events: the wrapper carries the tooltip. */}
        <span tabIndex={0} className="inline-flex">
          {button}
        </span>
      </TooltipTrigger>
      <TooltipContent id={`${data}-hint`}>{hint}</TooltipContent>
    </Tooltip>
  );
}

function ActionBar({
  disabled,
  canMove,
  canNote,
  canMessage,
  scheduling,
  selected,
  onAdvance,
  onHold,
  onReject,
  onNote,
  onSchedule,
  onTag,
  onSelect,
  onCompare,
}: {
  disabled: boolean;
  canMove: boolean;
  canNote: boolean;
  canMessage: boolean;
  scheduling: boolean;
  selected: boolean;
  onAdvance: () => void;
  onHold: () => void;
  onReject: () => void;
  onNote: () => void;
  onSchedule: () => void;
  onTag: () => void;
  onSelect: () => void;
  onCompare: () => void;
}) {
  const t = useTranslations("Screen");
  return (
    <footer
      className="flex flex-wrap items-center gap-2 border-t border-border bg-surface-sunken px-4 py-2.5 sm:px-6"
      aria-label={t("actions.label")}
      data-action-bar
    >
      <ActionButton primary data="advance" label={t("actions.advance")} keyName="a" icon={<ArrowRight className="rtl:rotate-180" aria-hidden />} onClick={onAdvance} disabled={disabled || !canMove} />
      <ActionButton data="hold" label={t("actions.hold")} keyName="h" icon={<Hand aria-hidden />} onClick={onHold} disabled={disabled || !canNote} />
      <ActionButton data="reject" label={t("actions.reject")} keyName="r" icon={<UserRoundX aria-hidden />} onClick={onReject} disabled={disabled || !canMove || !canMessage} />
      <ActionButton data="note" label={t("actions.note")} keyName="n" icon={<MessageSquareText aria-hidden />} onClick={onNote} disabled={disabled || !canNote} />
      <ActionButton
        data="schedule"
        label={t("actions.schedule")}
        keyName="s"
        icon={<CalendarClock aria-hidden />}
        onClick={onSchedule}
        disabled={disabled || !scheduling}
      />
      <ActionButton data="tag" label={t("actions.tag")} keyName="t" icon={<Tag aria-hidden />} onClick={onTag} disabled={disabled || !canNote} />
      <ActionButton data="select" label={t("actions.select")} keyName="x" icon={<ListChecks aria-hidden />} onClick={onSelect} disabled={disabled} />
      <ActionButton data="compare" label={t("actions.compare")} keyName="c" icon={<Columns3 aria-hidden />} onClick={onCompare} disabled={false} />
      <span className="ms-auto hidden text-xs text-muted-foreground xl:inline" aria-hidden>
        <Kbd>J</Kbd> <Kbd>K</Kbd> <Kbd>/</Kbd>
      </span>
      <span className="sr-only">{selected ? t("actions.select") : null}</span>
    </footer>
  );
}
