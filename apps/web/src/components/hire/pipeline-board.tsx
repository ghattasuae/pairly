// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowRight, Clock, EyeOff, Filter, Flag, Loader2, Mail, Search, Share2, UserRoundX } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type * as React from "react";

import { isTypingTarget } from "@/components/shell/shortcuts";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Checkbox, Select } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { useToast } from "@/components/ui/toaster";
import { Link } from "@/i18n/navigation";
import { errorKey } from "@/lib/action-result";
import * as hireActions from "@/lib/actions/hire";
import {
  actionKey,
  allTags,
  type BoardFlag,
  boardQuery,
  type BoardState,
  columns,
  FLAGS,
  filterCards,
  nextStage,
} from "@/lib/hire/board";
import type {
  HireBoard,
  HireBulkMessageSummary,
  HireBulkSummary,
  HireCard,
  HireMessage,
  HireMove,
  HireProfile,
  HireTeamMember,
  HireTemplate,
} from "@/lib/hire/types";
import { cn } from "@/lib/utils";

import { CandidatePanel, type CandidatePanelHandle } from "./candidate-panel";
import { ComposePanel } from "./compose-panel";

export type HireActions = typeof hireActions;

export interface PipelineBoardProps {
  board: HireBoard;
  initialState: BoardState;
  initialProfile: HireProfile | null;
  templates: HireTemplate[];
  team: HireTeamMember[];
  canMove: boolean;
  canNote: boolean;
  canMessage: boolean;
  totpEnabled: boolean;
  /** Overrides for stories and tests. Default to the server actions. */
  actions?: Partial<HireActions>;
}

type Panel =
  | { mode: "none" }
  | { mode: "loading"; id: string }
  | { mode: "profile"; profile: HireProfile }
  | { mode: "compose"; profile: HireProfile; message: HireMessage };

type BulkDialog =
  | { kind: "move"; stage: string; summary: HireBulkSummary }
  | { kind: "message"; template: string; summary: HireBulkMessageSummary };

const UNDO_MS = 10_000;

function anyDialogOpen(): boolean {
  return document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]') !== null;
}

/**
 * The pipeline of one job (brief 3.4.1 and 5.3): a kanban board with the candidate's
 * profile beside it, never a new page.
 *
 * - Move: the arrow on a card (one click), A on the selected card (one key), drag and
 *   drop, or the panel. An undo toast stays 10 seconds. Moving to Rejected drafts the
 *   rejection and opens it: one click submits it to the approval queue (R, then a click).
 * - Keyboard: J/K select, Enter opens, A advances, R rejects, N writes a note, X selects
 *   for a bulk action, / searches, Esc closes the panel.
 * - Bulk: select cards, then move them (undo for the batch) or send a status update; a
 *   summary comes first whenever a rejection draft or an email would follow.
 * - Filters, the open candidate and the scroll position live in the URL.
 */
export function PipelineBoard({
  board: initialBoard,
  initialState,
  initialProfile,
  templates,
  team,
  canMove,
  canNote,
  canMessage,
  totpEnabled,
  actions: overrides,
}: PipelineBoardProps) {
  const t = useTranslations("Hire");
  const tScreen = useTranslations("Screen");
  const tErrors = useTranslations("Errors");
  const locale = useLocale();
  const toast = useToast();
  const actions = useMemo(() => ({ ...hireActions, ...overrides }), [overrides]);
  const jobId = initialBoard.job_id;

  const [board, setBoard] = useState(initialBoard);
  const [state, setState] = useState(initialState);
  const [selected, setSelected] = useState<string | null>(
    initialState.candidate ?? initialBoard.cards[0]?.application_id ?? null,
  );
  const [panel, setPanel] = useState<Panel>(initialProfile ? { mode: "profile", profile: initialProfile } : { mode: "none" });
  const [bulk, setBulk] = useState<Set<string>>(new Set());
  const [bulkStage, setBulkStage] = useState("");
  const [dialog, setDialog] = useState<BulkDialog | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const panelRef = useRef<CandidatePanelHandle | null>(null);
  const noteAfterOpen = useRef(false);

  const visible = useMemo(() => filterCards(board.cards, state), [board.cards, state]);
  const grouped = useMemo(() => columns(board.stages, visible), [board.stages, visible]);
  const ordered = useMemo(() => board.stages.flatMap((s) => grouped.get(s.key) ?? []), [board.stages, grouped]);
  const tags = useMemo(() => allTags(board.cards), [board.cards]);
  const stageLabel = useCallback(
    (key: string) => {
      const stage = board.stages.find((s) => s.key === key);
      return stage ? (locale === "ar" && stage.label_ar ? stage.label_ar : stage.label) : key;
    },
    [board.stages, locale],
  );

  // ------------------------------------------------------------ URL and scroll

  useEffect(() => {
    const url = `/${locale}/hire/jobs/${jobId}/pipeline${boardQuery(state)}`;
    if (`${window.location.pathname}${window.location.search}` !== url) window.history.replaceState(null, "", url);
  }, [state, locale, jobId]);

  useEffect(() => {
    if (scroller.current && initialState.x) scroller.current.scrollLeft = initialState.x;
    if (initialState.y) window.scrollTo({ top: initialState.y });
    // Restore once, on the first render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let timer: number | undefined;
    const remember = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        setState((s) => ({ ...s, x: Math.max(0, Math.abs(scroller.current?.scrollLeft ?? 0)), y: window.scrollY }));
      }, 250);
    };
    const el = scroller.current;
    el?.addEventListener("scroll", remember, { passive: true });
    window.addEventListener("scroll", remember, { passive: true });
    return () => {
      window.clearTimeout(timer);
      el?.removeEventListener("scroll", remember);
      window.removeEventListener("scroll", remember);
    };
  }, []);

  // ------------------------------------------------------------ data

  const refreshBoard = useCallback(async () => {
    const result = await actions.loadBoard(jobId);
    if (result.ok) setBoard(result.board);
  }, [actions, jobId]);

  const loadProfile = useCallback(
    async (id: string): Promise<HireProfile | null> => {
      const result = await actions.loadProfile(id);
      if (!result.ok) {
        setError(result.code);
        return null;
      }
      return result.profile;
    },
    [actions],
  );

  const open = useCallback(
    async (id: string, focusNote = false) => {
      setSelected(id);
      setState((s) => ({ ...s, candidate: id }));
      noteAfterOpen.current = focusNote;
      setPanel((p) => (p.mode !== "none" && "profile" in p && p.profile.application_id === id ? p : { mode: "loading", id }));
      const profile = await loadProfile(id);
      if (profile) setPanel((p) => (p.mode === "compose" && p.profile.application_id === id ? p : { mode: "profile", profile }));
      else setPanel({ mode: "none" });
    },
    [loadProfile],
  );

  useEffect(() => {
    if (noteAfterOpen.current && panel.mode === "profile") {
      noteAfterOpen.current = false;
      panelRef.current?.focusNote();
    }
  }, [panel]);

  const close = useCallback(() => {
    setPanel({ mode: "none" });
    setState((s) => ({ ...s, candidate: null }));
  }, []);

  const reloadOpen = useCallback(
    async (id: string) => {
      const profile = await loadProfile(id);
      if (profile) setPanel((p) => (p.mode === "profile" && p.profile.application_id === id ? { mode: "profile", profile } : p));
    },
    [loadProfile],
  );

  // ------------------------------------------------------------ moves

  const setCardStage = (ids: string[], stage: string) =>
    setBoard((b) => ({
      ...b,
      cards: b.cards.map((c) => (ids.includes(c.application_id) ? { ...c, stage_key: stage } : c)),
    }));

  const undo = useCallback(
    async (moveIds: string[]) => {
      const result = await actions.undoMoves(moveIds);
      if (!result.ok) {
        toast.show({ tone: "error", title: t("undoFailed"), body: tErrors(errorKey(result.code) as "generic") });
        return;
      }
      toast.show({ tone: "info", title: t("undone"), duration: 3000 });
      await refreshBoard();
      const openId = state.candidate;
      if (openId) await reloadOpen(openId);
    },
    [actions, refreshBoard, reloadOpen, state.candidate, t, tErrors, toast],
  );

  const afterMove = useCallback(
    (moves: HireMove[], label: string) => {
      toast.show({
        tone: "success",
        title: label,
        duration: UNDO_MS,
        action: { label: t("undo"), onClick: () => void undo(moves.map((m) => m.move_id)) },
      });
    },
    [t, toast, undo],
  );

  const openDraft = useCallback(
    async (applicationId: string, draftId: string) => {
      const [profile, message] = await Promise.all([loadProfile(applicationId), actions.loadMessage(draftId)]);
      if (profile && message.ok) {
        setSelected(applicationId);
        setState((s) => ({ ...s, candidate: applicationId }));
        setPanel({ mode: "compose", profile, message: message.message });
      }
    },
    [actions, loadProfile],
  );

  const moveTo = useCallback(
    async (card: HireCard, stage: string) => {
      if (!canMove || card.stage_key === stage) return;
      const before = card.stage_key;
      setError(null);
      setCardStage([card.application_id], stage);
      setBusy(true);
      const result = await actions.moveCandidate({
        applicationId: card.application_id,
        toStage: stage,
        idempotencyKey: actionKey(),
      });
      setBusy(false);
      if (!result.ok) {
        setCardStage([card.application_id], before);
        setError(result.code);
        return;
      }
      afterMove([result.move], t("moved", { name: card.label, stage: stageLabel(stage) }));
      if (result.move.draft_id) await openDraft(card.application_id, result.move.draft_id);
      else if (state.candidate === card.application_id) await reloadOpen(card.application_id);
      void refreshBoard();
    },
    [actions, afterMove, canMove, openDraft, refreshBoard, reloadOpen, stageLabel, state.candidate, t],
  );

  const advance = useCallback(
    async (card: HireCard) => {
      const next = nextStage(board.stages, card.stage_key);
      if (!next || !canMove) return;
      setError(null);
      setCardStage([card.application_id], next.key);
      setBusy(true);
      const result = await actions.advanceCandidate({ applicationId: card.application_id, idempotencyKey: actionKey() });
      setBusy(false);
      if (!result.ok) {
        setCardStage([card.application_id], card.stage_key);
        setError(result.code);
        return;
      }
      afterMove([result.move], t("moved", { name: card.label, stage: stageLabel(result.move.to_stage) }));
      if (state.candidate === card.application_id) await reloadOpen(card.application_id);
      void refreshBoard();
    },
    [actions, afterMove, board.stages, canMove, refreshBoard, reloadOpen, stageLabel, state.candidate, t],
  );

  const reject = useCallback(
    (card: HireCard) => {
      if (card.stage_key === "rejected") {
        if (card.open_message_id) void openDraft(card.application_id, card.open_message_id);
        return;
      }
      void moveTo(card, "rejected");
    },
    [moveTo, openDraft],
  );

  // ------------------------------------------------------------ bulk

  const toggleBulk = (id: string) =>
    setBulk((all) => {
      const copy = new Set(all);
      if (copy.has(id)) copy.delete(id);
      else copy.add(id);
      return copy;
    });

  const bulkMove = async (stage: string) => {
    const idsList = [...bulk];
    if (!stage || idsList.length === 0) return;
    setError(null);
    if (stage === "rejected") {
      const preview = await actions.bulkPreview({ jobId, applicationIds: idsList, toStage: stage });
      if (!preview.ok) {
        setError(preview.code);
        return;
      }
      setDialog({ kind: "move", stage, summary: preview.summary });
      return;
    }
    await runBulkMove(stage, idsList);
  };

  const runBulkMove = async (stage: string, idsList: string[]) => {
    setBusy(true);
    const result = await actions.bulkMove({ jobId, applicationIds: idsList, toStage: stage, idempotencyKey: actionKey() });
    setBusy(false);
    setDialog(null);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    setBulk(new Set());
    afterMove(result.result.moves, t("bulkMoved", { count: result.result.moves.length, stage: stageLabel(stage) }));
    await refreshBoard();
  };

  const bulkUpdate = async () => {
    const idsList = [...bulk];
    if (idsList.length === 0) return;
    const preview = await actions.bulkMessagePreview({
      jobId,
      applicationIds: idsList,
      templateKey: "hire.candidate.status_update",
    });
    if (!preview.ok) {
      setError(preview.code);
      return;
    }
    setDialog({ kind: "message", template: "hire.candidate.status_update", summary: preview.summary });
  };

  const runBulkMessage = async (template: string) => {
    setBusy(true);
    const result = await actions.bulkMessageSend({
      jobId,
      applicationIds: [...bulk],
      templateKey: template,
      idempotencyKey: actionKey(),
    });
    setBusy(false);
    setDialog(null);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    setBulk(new Set());
    toast.show({ tone: "success", title: t("bulkSent", { count: result.sent }) });
    await refreshBoard();
  };

  // ------------------------------------------------------------ panel callbacks

  const openProfile = panel.mode === "profile" || panel.mode === "compose" ? panel.profile : null;

  const compose = async (templateKey: string) => {
    if (!openProfile) return;
    setBusy(true);
    const result = await actions.createMessage({ applicationId: openProfile.application_id, templateKey });
    setBusy(false);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    setPanel({ mode: "compose", profile: openProfile, message: result.message });
  };

  const openMessage = async (messageId: string) => {
    if (!openProfile) return;
    const result = await actions.loadMessage(messageId);
    if (result.ok) setPanel({ mode: "compose", profile: openProfile, message: result.message });
    else setError(result.code);
  };

  const composeDone = async () => {
    if (!openProfile) return;
    const profile = await loadProfile(openProfile.application_id);
    setPanel(profile ? { mode: "profile", profile } : { mode: "none" });
    void refreshBoard();
  };

  // ------------------------------------------------------------ keyboard

  const cardOf = useCallback((id: string | null) => board.cards.find((c) => c.application_id === id) ?? null, [board.cards]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target) || anyDialogOpen()) return;
      const key = event.key;
      const index = ordered.findIndex((c) => c.application_id === selected);
      const current = cardOf(selected);
      const go = (delta: number) => {
        const next = ordered[Math.min(ordered.length - 1, Math.max(0, (index < 0 ? -delta : index) + delta))];
        if (!next) return;
        setSelected(next.application_id);
        document.querySelector<HTMLElement>(`[data-card="${CSS.escape(next.application_id)}"]`)?.focus();
        if (panel.mode !== "none") void open(next.application_id);
      };
      if (key === "j" || key === "ArrowDown") {
        event.preventDefault();
        go(1);
      } else if (key === "k" || key === "ArrowUp") {
        event.preventDefault();
        go(-1);
      } else if ((key === "Enter" || key === "o") && current) {
        event.preventDefault();
        void open(current.application_id);
      } else if ((key === "a" || key === "A") && current) {
        event.preventDefault();
        void advance(current);
      } else if ((key === "r" || key === "R") && current) {
        event.preventDefault();
        reject(current);
      } else if ((key === "n" || key === "N") && current && canNote) {
        event.preventDefault();
        void open(current.application_id, true);
      } else if ((key === "x" || key === "X") && current) {
        event.preventDefault();
        toggleBulk(current.application_id);
      } else if (key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
      } else if (key === "Escape" && panel.mode !== "none") {
        event.preventDefault();
        close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advance, canNote, cardOf, close, open, ordered, panel.mode, reject, selected]);

  // ------------------------------------------------------------ render

  const setFilter = (patch: Partial<BoardState>) => setState((s) => ({ ...s, ...patch }));
  const panelOpen = panel.mode !== "none";
  const returnPath = `/${locale}/hire/jobs/${jobId}/pipeline`;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-3 px-5 pt-6 pb-3 sm:px-8">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="space-y-1">
            <p className="eyebrow">{t("pipeline")}</p>
            <h1 className="font-display text-3xl leading-tight tracking-tight">{board.job_title}</h1>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Link href={`/hire/jobs/${jobId}/screen`} className="text-sm text-primary underline-offset-4 hover:underline" data-screen-link>
              {tScreen("open")}
            </Link>
            {board.blind_mode && (
              <Badge variant="signal" title={t("blindHelp")}>
                <EyeOff className="size-3" aria-hidden />
                {t("blindOn")}
              </Badge>
            )}
            <span className="hidden items-center gap-1 lg:flex" aria-hidden>
              <Kbd>J</Kbd>
              <Kbd>K</Kbd> {t("keys.select")} <Kbd>A</Kbd> {t("keys.advanceShort")} <Kbd>R</Kbd> {t("keys.rejectShort")}{" "}
              <Kbd>N</Kbd> {t("keys.noteShort")} <Kbd>X</Kbd> {t("keys.bulkShort")}
            </span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2" role="search">
          <div className="relative min-w-48 flex-1 sm:max-w-xs">
            <Search className="pointer-events-none absolute start-2.5 top-2.5 size-4 text-muted-foreground" aria-hidden />
            <Input
              ref={searchRef}
              value={state.q}
              onChange={(e) => setFilter({ q: e.target.value })}
              placeholder={t("filters.search")}
              aria-label={t("filters.search")}
              className="h-9 ps-8"
            />
          </div>
          <Select
            aria-label={t("filters.tag")}
            value={state.tag ?? ""}
            onChange={(e) => setFilter({ tag: e.target.value || null })}
            className="h-9 w-auto text-sm"
          >
            <option value="">{t("filters.anyTag")}</option>
            {tags.map((tag) => (
              <option key={tag} value={tag}>
                {tag}
              </option>
            ))}
          </Select>
          <div className="flex items-center gap-1" role="group" aria-label={t("filters.flags")}>
            <Filter className="size-4 text-muted-foreground" aria-hidden />
            {FLAGS.map((flag) => (
              <button
                key={flag}
                type="button"
                aria-pressed={state.flag === flag}
                onClick={() => setFilter({ flag: state.flag === flag ? null : (flag as BoardFlag) })}
                className={cn(
                  "rounded-full border px-2.5 py-1 text-xs",
                  state.flag === flag ? "border-primary bg-primary text-primary-foreground" : "hover:bg-accent",
                )}
              >
                {t(`filters.${flag}`)}
              </button>
            ))}
          </div>
        </div>
        {bulk.size > 0 && (
          <div
            className="flex flex-wrap items-center gap-2 rounded-lg border bg-surface px-3 py-2 text-sm shadow-soft"
            role="region"
            aria-label={t("bulk.title")}
          >
            <span className="font-medium">{t("bulk.selected", { count: bulk.size })}</span>
            <Select
              aria-label={t("bulk.moveTo")}
              value={bulkStage}
              onChange={(e) => setBulkStage(e.target.value)}
              className="h-8 w-auto text-sm"
            >
              <option value="">{t("bulk.moveTo")}</option>
              {board.stages.map((s) => (
                <option key={s.key} value={s.key}>
                  {stageLabel(s.key)}
                </option>
              ))}
            </Select>
            <Button type="button" size="sm" disabled={!bulkStage || busy || !canMove} onClick={() => void bulkMove(bulkStage)}>
              {t("bulk.move")}
            </Button>
            {canMessage && (
              <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void bulkUpdate()}>
                <Mail aria-hidden />
                {t("bulk.update")}
              </Button>
            )}
            <Button type="button" size="sm" variant="ghost" onClick={() => setBulk(new Set())}>
              {t("bulk.clear")}
            </Button>
          </div>
        )}
        {error === "second_factor_required" ? (
          <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} compact />
        ) : error ? (
          <p role="alert" className="text-sm text-destructive">
            {tErrors(errorKey(error) as "generic")}
          </p>
        ) : null}
      </div>

      <div className="flex min-h-0 flex-1 gap-0 border-t">
        <div
          ref={scroller}
          className="min-w-0 flex-1 overflow-x-auto px-5 py-4 sm:px-8"
          data-board
          aria-label={t("boardLabel")}
        >
          <ol className="flex min-h-[60vh] gap-3">
            {board.stages.map((stage) => {
              const cards = grouped.get(stage.key) ?? [];
              return (
                <li
                  key={stage.key}
                  className={cn(
                    "flex w-64 shrink-0 flex-col rounded-xl border bg-surface-sunken/50 transition-colors",
                    dragOver === stage.key && "border-primary bg-accent/50",
                    stage.kind !== "active" && "bg-surface-sunken/25",
                  )}
                  data-column={stage.key}
                  aria-label={stageLabel(stage.key)}
                  onDragOver={(e) => {
                    if (!canMove) return;
                    e.preventDefault();
                    setDragOver(stage.key);
                  }}
                  onDragLeave={() => setDragOver((s) => (s === stage.key ? null : s))}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDragOver(null);
                    const card = cardOf(e.dataTransfer.getData("text/x-pairly-card"));
                    if (!card) return;
                    if (stage.key === "rejected") reject(card);
                    else void moveTo(card, stage.key);
                  }}
                >
                  <h2 className="flex items-center justify-between gap-2 px-3 pt-3 pb-2 text-sm font-medium">
                    <span>{stageLabel(stage.key)}</span>
                    <span className="rounded-full bg-background px-2 text-xs text-muted-foreground tabular-nums">
                      {cards.length}
                    </span>
                  </h2>
                  <ul className="flex flex-1 flex-col gap-2 px-2 pb-3">
                    {cards.map((card) => (
                      <CardItem
                        key={card.application_id}
                        card={card}
                        next={nextStage(board.stages, card.stage_key)}
                        nextLabel={(key) => stageLabel(key)}
                        selected={selected === card.application_id}
                        open={state.candidate === card.application_id && panelOpen}
                        inBulk={bulk.has(card.application_id)}
                        canMove={canMove}
                        busy={busy}
                        onSelect={() => void open(card.application_id)}
                        onFocus={() => setSelected(card.application_id)}
                        onAdvance={() => void advance(card)}
                        onToggleBulk={() => toggleBulk(card.application_id)}
                      />
                    ))}
                  </ul>
                </li>
              );
            })}
          </ol>
        </div>

        {panelOpen && (
          <div
            className="fixed inset-0 z-40 overflow-hidden border-s bg-background md:static md:z-auto md:w-[26rem] md:shrink-0 xl:w-[30rem]"
            data-side-panel
          >
            {panel.mode === "loading" ? (
              <div className="flex h-full items-center justify-center text-muted-foreground" role="status">
                <Loader2 className="animate-spin" aria-hidden />
                <span className="sr-only">{t("loading")}</span>
              </div>
            ) : panel.mode === "compose" ? (
              <div className="h-full overflow-y-auto px-5 py-5">
                <ComposePanel
                  key={panel.message.id}
                  message={panel.message}
                  onBack={() => setPanel({ mode: "profile", profile: panel.profile })}
                  onDone={() => void composeDone()}
                  onError={(code) => setError(code)}
                  actions={{
                    edit: actions.editMessage,
                    ai: actions.aiDraftMessage,
                    send: actions.sendMessage,
                    submit: actions.submitMessage,
                    cancel: actions.cancelMessage,
                  }}
                />
              </div>
            ) : panel.mode === "profile" ? (
              <CandidatePanel
                ref={panelRef}
                profile={panel.profile}
                templates={templates}
                team={team}
                canMove={canMove}
                canNote={canNote}
                canMessage={canMessage}
                busy={busy}
                stageLabel={stageLabel}
                onClose={close}
                onAdvance={() => {
                  const card = cardOf(panel.profile.application_id);
                  if (card) void advance(card);
                }}
                onReject={() => {
                  const card = cardOf(panel.profile.application_id);
                  if (card) reject(card);
                }}
                onAddNote={async (body, mentions) => {
                  const result = await actions.addNote({ applicationId: panel.profile.application_id, body, mentions });
                  if (!result.ok) {
                    setError(result.code);
                    return false;
                  }
                  toast.show({ tone: "success", title: t("noteAdded"), duration: 3000 });
                  await reloadOpen(panel.profile.application_id);
                  return true;
                }}
                onSetTags={async (next) => {
                  const result = await actions.setTags(panel.profile.application_id, next);
                  if (!result.ok) {
                    setError(result.code);
                    return false;
                  }
                  await reloadOpen(panel.profile.application_id);
                  void refreshBoard();
                  return true;
                }}
                onCompose={(key) => void compose(key)}
                onOpenMessage={(id) => void openMessage(id)}
              />
            ) : null}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={dialog !== null}
        onOpenChange={(o) => {
          if (!o) setDialog(null);
        }}
        title={dialog?.kind === "move" ? t("bulk.confirmMoveTitle", { stage: stageLabel(dialog.stage) }) : t("bulk.confirmSendTitle")}
        description={dialog ? <BulkSummaryText dialog={dialog} /> : null}
        confirmLabel={dialog?.kind === "move" ? t("bulk.confirmMove") : t("bulk.confirmSend")}
        tone={dialog?.kind === "move" && dialog.stage === "rejected" ? "destructive" : "default"}
        pending={busy}
        confirmDisabled={dialog?.kind === "message" && dialog.summary.will_send === 0}
        onConfirm={() => {
          if (dialog?.kind === "move") void runBulkMove(dialog.stage, [...bulk]);
          else if (dialog?.kind === "message") void runBulkMessage(dialog.template);
        }}
      />
    </div>
  );
}

function BulkSummaryText({ dialog }: { dialog: BulkDialog }) {
  const t = useTranslations("Hire");
  if (dialog.kind === "move") {
    const s = dialog.summary;
    return (
      <span className="block space-y-1" data-bulk-summary>
        <span className="block">{t("bulk.summaryMove", { count: s.will_move, total: s.total })}</span>
        {s.drafts_to_create > 0 && <span className="block">{t("bulk.summaryDrafts", { count: s.drafts_to_create })}</span>}
        {s.already_there.length > 0 && (
          <span className="block">{t("bulk.summaryAlready", { count: s.already_there.length })}</span>
        )}
        <span className="block font-medium">{t("bulk.summaryNothingSent")}</span>
      </span>
    );
  }
  const s = dialog.summary;
  return (
    <span className="block space-y-1" data-bulk-summary>
      <span className="block">{t("bulk.summarySend", { count: s.will_send, total: s.total })}</span>
      {s.opted_out > 0 && <span className="block">{t("bulk.summaryOptedOut", { count: s.opted_out })}</span>}
    </span>
  );
}

function CardItem({
  card,
  next,
  nextLabel,
  selected,
  open,
  inBulk,
  canMove,
  busy,
  onSelect,
  onFocus,
  onAdvance,
  onToggleBulk,
}: {
  card: HireCard;
  next: { key: string } | null;
  nextLabel: (key: string) => string;
  selected: boolean;
  open: boolean;
  inBulk: boolean;
  canMove: boolean;
  busy: boolean;
  onSelect: () => void;
  onFocus: () => void;
  onAdvance: () => void;
  onToggleBulk: () => void;
}) {
  const t = useTranslations("Hire");
  const terminal = ["hired", "rejected", "withdrawn"].includes(card.stage_key);
  return (
    <li>
      <article
        tabIndex={0}
        data-card={card.application_id}
        data-stage={card.stage_key}
        aria-current={open ? "true" : undefined}
        aria-label={card.label}
        draggable={canMove}
        onDragStart={(e: React.DragEvent) => {
          e.dataTransfer.setData("text/x-pairly-card", card.application_id);
          e.dataTransfer.effectAllowed = "move";
        }}
        onFocus={onFocus}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button, input, a")) return;
          onSelect();
        }}
        className={cn(
          "group relative cursor-pointer rounded-lg border bg-background p-3 text-sm shadow-xs outline-none transition-[box-shadow,border-color] hover:border-primary/40 focus-visible:ring-[3px] focus-visible:ring-ring/30",
          selected && "border-primary/60",
          open && "ring-2 ring-primary/40",
          inBulk && "bg-accent/60",
        )}
      >
        <div className="flex items-start gap-2">
          <Checkbox
            checked={inBulk}
            onChange={onToggleBulk}
            aria-label={t("card.select", { name: card.label })}
            className="mt-0.5"
          />
          <div className="min-w-0 flex-1 space-y-1">
            <p className="flex items-center gap-1 font-medium leading-snug">
              {card.identity_hidden && <EyeOff className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />}
              <span className="truncate">{card.label}</span>
            </p>
            <p className="font-mono text-[0.7rem] text-muted-foreground" dir="ltr">
              {card.reference}
            </p>
          </div>
          {canMove && next && !terminal && (
            <button
              type="button"
              onClick={onAdvance}
              disabled={busy}
              aria-label={t("card.advance", { name: card.label, stage: nextLabel(next.key) })}
              title={t("card.advanceTitle", { stage: nextLabel(next.key) })}
              className="flex size-7 shrink-0 items-center justify-center rounded-md border text-muted-foreground hover:border-primary hover:bg-primary hover:text-primary-foreground disabled:opacity-50"
              data-advance
            >
              <ArrowRight className="size-4 rtl:rotate-180" aria-hidden />
            </button>
          )}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {card.sla_overdue && (
            <Badge variant="signal" title={t("card.waitingTitle", { days: card.waiting_days })}>
              <Clock className="size-3" aria-hidden />
              {t("card.waiting", { days: card.waiting_days })}
            </Badge>
          )}
          {card.needs_human_review && (
            <Badge variant="outline" title={card.review_reasons.join(", ")}>
              <Flag className="size-3" aria-hidden />
              {t("card.needsReview")}
            </Badge>
          )}
          {card.referred && (
            <Badge variant="outline">
              <Share2 className="size-3" aria-hidden />
              {t("card.referred")}
            </Badge>
          )}
          {card.open_message && (
            <Badge variant="outline" data-open-message-state={card.open_message}>
              {card.open_message === "awaiting_approval" ? (
                <UserRoundX className="size-3" aria-hidden />
              ) : (
                <Mail className="size-3" aria-hidden />
              )}
              {t(`messageStatus.${card.open_message}` as "messageStatus.draft")}
            </Badge>
          )}
          {card.tags.slice(0, 3).map((tag) => (
            <Badge key={tag} variant="default" className="font-normal">
              {tag}
            </Badge>
          ))}
        </div>
      </article>
    </li>
  );
}
