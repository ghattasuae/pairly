// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import {
  ArrowRight,
  AtSign,
  CalendarClock,
  EyeOff,
  FileText,
  Flag,
  Loader2,
  Mail,
  MessageSquareText,
  Send,
  Tag,
  UserRoundX,
  X,
} from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { forwardRef, useId, useImperativeHandle, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { Link } from "@/i18n/navigation";
import type { HireEvent, HireProfile, HireTeamMember, HireTemplate } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

export interface CandidatePanelHandle {
  focusNote: () => void;
}

export interface CandidatePanelProps {
  profile: HireProfile;
  templates: HireTemplate[];
  team: HireTeamMember[];
  canMove: boolean;
  canNote: boolean;
  canMessage: boolean;
  busy: boolean;
  onClose: () => void;
  onAdvance: () => void;
  onReject: () => void;
  onAddNote: (body: string, mentions: string[]) => Promise<boolean>;
  onSetTags: (tags: string[]) => Promise<boolean>;
  /** Start a message from a template (one click: the draft opens in the compose panel). */
  onCompose: (templateKey: string) => void;
  /** Open an existing draft or message. */
  onOpenMessage: (messageId: string) => void;
  /** A stage's label in the reader's language. */
  stageLabel: (key: string) => string;
  /** Open the scheduling panel (shown when the viewer may schedule interviews). */
  onSchedule?: (() => void) | undefined;
}

const OPEN = new Set(["draft", "changes_requested", "submitted", "awaiting_approval"]);

/**
 * The candidate profile beside the board (brief 3.4.2 and 5.3: never lose your place):
 * stage and the next step, a note box that is always there, templates one click away,
 * tags, contact details and files (hidden in blind mode until the stage reveals them),
 * and the timeline of everything that happened.
 */
export const CandidatePanel = forwardRef<CandidatePanelHandle, CandidatePanelProps>(function CandidatePanel(
  {
    profile,
    templates,
    team,
    canMove,
    canNote,
    canMessage,
    busy,
    onClose,
    onAdvance,
    onReject,
    onAddNote,
    onSetTags,
    onCompose,
    onOpenMessage,
    stageLabel,
    onSchedule,
  },
  ref,
) {
  const t = useTranslations("Hire");
  const tSchedule = useTranslations("Scheduling");
  const format = useFormatter();
  const locale = useLocale();
  const ids = useId();
  const noteRef = useRef<HTMLTextAreaElement | null>(null);
  const [note, setNote] = useState("");
  const [mentions, setMentions] = useState<string[]>([]);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [tagInput, setTagInput] = useState("");

  useImperativeHandle(ref, () => ({ focusNote: () => noteRef.current?.focus() }), []);

  const suggested = templates.filter((tpl) => !tpl.sensitive && tpl.suggested_for.includes(profile.stage_key));
  const quick = (suggested.length > 0 ? suggested : templates.filter((tpl) => !tpl.sensitive)).slice(0, 3);
  const others = templates.filter((tpl) => !quick.includes(tpl));
  const openMessages = profile.messages.filter((m) => OPEN.has(m.status));
  const terminal = ["hired", "rejected", "withdrawn"].includes(profile.stage_key);

  const saveNote = async () => {
    if (!note.trim()) {
      noteRef.current?.focus();
      return;
    }
    setSaving(true);
    const ok = await onAddNote(note.trim(), mentions);
    setSaving(false);
    if (ok) {
      setNote("");
      setMentions([]);
    }
  };

  const addTag = async () => {
    const value = tagInput.trim();
    if (!value) return;
    if (await onSetTags([...profile.tags, value])) setTagInput("");
  };

  return (
    <aside
      aria-labelledby={`${ids}-name`}
      className="flex h-full min-h-0 flex-col"
      data-candidate-panel={profile.application_id}
    >
      <header className="space-y-3 border-b px-5 pt-5 pb-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <p className="eyebrow">{profile.job_title}</p>
            <h2 id={`${ids}-name`} className="font-display text-2xl leading-tight break-words">
              {profile.label}
            </h2>
            <p className="font-mono text-xs text-muted-foreground" dir="ltr">
              {profile.reference}
            </p>
          </div>
          <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label={t("panel.close")}>
            <X aria-hidden />
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={terminal ? "outline" : "default"} data-stage={profile.stage_key}>
            {profile.stage_label}
          </Badge>
          {profile.needs_human_review && (
            <Badge variant="signal">
              <Flag className="size-3" aria-hidden />
              {t("card.needsReview")}
            </Badge>
          )}
          {profile.referred_by && <Badge variant="outline">{t("panel.referredBy", { name: profile.referred_by })}</Badge>}
        </div>
        {canMove && (
          <div className="flex flex-wrap gap-2">
            {profile.next_stage && !terminal && (
              <Button type="button" size="sm" onClick={onAdvance} disabled={busy} title={t("keys.advance")}>
                <ArrowRight aria-hidden className="rtl:rotate-180" />
                {t("panel.moveTo", { stage: profile.next_stage.label })}
                <Kbd className="ms-1 border-primary-foreground/30 bg-transparent text-primary-foreground/80">A</Kbd>
              </Button>
            )}
            {profile.stage_key !== "rejected" && (
              <Button type="button" size="sm" variant="outline" onClick={onReject} disabled={busy} title={t("keys.reject")}>
                <UserRoundX aria-hidden />
                {t("panel.reject")}
                <Kbd className="ms-1">R</Kbd>
              </Button>
            )}
          </div>
        )}
        {onSchedule && !terminal && (
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" onClick={onSchedule} data-schedule title={tSchedule("title")}>
              <CalendarClock aria-hidden />
              {tSchedule("open")}
              <Kbd className="ms-1">S</Kbd>
            </Button>
          </div>
        )}
      </header>

      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-4">
        {profile.identity_hidden && (
          <p role="note" className="flex items-start gap-2 rounded-md border bg-surface-sunken/60 px-3 py-2 text-sm">
            <EyeOff className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
            {t("panel.blind")}
          </p>
        )}

        {canNote && (
          <section aria-labelledby={`${ids}-note`} className="space-y-2">
            <h3 id={`${ids}-note`} className="eyebrow">
              {t("panel.note")}
            </h3>
            <Textarea
              ref={noteRef}
              rows={2}
              dir="auto"
              value={note}
              maxLength={4000}
              placeholder={t("panel.notePlaceholder")}
              aria-label={t("panel.note")}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void saveNote();
                }
              }}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" onClick={() => void saveNote()} disabled={saving} data-add-note>
                {saving ? <Loader2 className="animate-spin" aria-hidden /> : <MessageSquareText aria-hidden />}
                {t("panel.addNote")}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-expanded={mentionOpen}
                onClick={() => setMentionOpen((v) => !v)}
              >
                <AtSign aria-hidden />
                {mentions.length > 0 ? t("panel.mentioning", { count: mentions.length }) : t("panel.mention")}
              </Button>
            </div>
            {mentionOpen && (
              <ul className="flex flex-wrap gap-1.5" aria-label={t("panel.mention")}>
                {team.map((member) => {
                  const on = mentions.includes(member.id);
                  return (
                    <li key={member.id}>
                      <button
                        type="button"
                        aria-pressed={on}
                        onClick={() =>
                          setMentions((all) => (on ? all.filter((m) => m !== member.id) : [...all, member.id]))
                        }
                        className={cn(
                          "rounded-full border px-2.5 py-0.5 text-xs",
                          on ? "border-primary bg-primary text-primary-foreground" : "hover:bg-accent",
                        )}
                      >
                        @{member.name}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        )}

        {canMessage && (
          <section aria-labelledby={`${ids}-msg`} className="space-y-2">
            <h3 id={`${ids}-msg`} className="eyebrow">
              {t("panel.message")}
            </h3>
            {openMessages.length > 0 && (
              <ul className="space-y-1.5">
                {openMessages.map((m) => (
                  <li key={m.id}>
                    <button
                      type="button"
                      onClick={() => onOpenMessage(m.id)}
                      className="flex w-full items-center justify-between gap-2 rounded-md border bg-signal-soft/50 px-3 py-2 text-start text-sm hover:bg-signal-soft"
                      data-open-message={m.id}
                    >
                      <span className="truncate font-medium">{m.label}</span>
                      <Badge variant="outline">{t(`messageStatus.${m.status}` as "messageStatus.draft")}</Badge>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="flex flex-wrap gap-2">
              {quick.map((tpl) => (
                <Button
                  key={tpl.key}
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onCompose(tpl.key)}
                  disabled={busy}
                  data-template={tpl.key}
                >
                  <Mail aria-hidden />
                  {locale === "ar" ? tpl.label_ar : tpl.label}
                </Button>
              ))}
            </div>
            {others.length > 0 && (
              <Select
                aria-label={t("panel.moreTemplates")}
                value=""
                onChange={(e) => {
                  if (e.target.value) onCompose(e.target.value);
                }}
                className="h-9 text-sm"
              >
                <option value="">{t("panel.moreTemplates")}</option>
                {others.map((tpl) => (
                  <option key={tpl.key} value={tpl.key}>
                    {(locale === "ar" ? tpl.label_ar : tpl.label) + (tpl.sensitive ? ` (${t("compose.needsApproval")})` : "")}
                  </option>
                ))}
              </Select>
            )}
            {!profile.updates_email && <p className="text-xs text-muted-foreground">{t("panel.optedOut")}</p>}
          </section>
        )}

        <section aria-labelledby={`${ids}-tags`} className="space-y-2">
          <h3 id={`${ids}-tags`} className="eyebrow">
            {t("panel.tags")}
          </h3>
          <ul className="flex flex-wrap gap-1.5">
            {profile.tags.map((tag) => (
              <li key={tag}>
                <Badge variant="outline" className="gap-1">
                  <Tag className="size-3" aria-hidden />
                  {tag}
                  {canNote && (
                    <button
                      type="button"
                      aria-label={t("panel.removeTag", { tag })}
                      onClick={() => void onSetTags(profile.tags.filter((x) => x !== tag))}
                      className="rounded-full hover:text-foreground"
                    >
                      <X className="size-3" aria-hidden />
                    </button>
                  )}
                </Badge>
              </li>
            ))}
          </ul>
          {canNote && (
            <Input
              value={tagInput}
              maxLength={32}
              placeholder={t("panel.addTag")}
              aria-label={t("panel.addTag")}
              className="h-8 text-sm"
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void addTag();
                }
              }}
            />
          )}
        </section>

        {profile.contact && (
          <section aria-labelledby={`${ids}-contact`} className="space-y-2 text-sm">
            <h3 id={`${ids}-contact`} className="eyebrow">
              {t("panel.contact")}
            </h3>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              {(["email", "phone", "location"] as const).map((k) =>
                profile.contact?.[k] ? (
                  <div key={k} className="contents">
                    <dt className="text-muted-foreground">{t(`panel.${k}`)}</dt>
                    <dd className="break-all" dir={k === "location" ? "auto" : "ltr"}>
                      {String(profile.contact[k])}
                    </dd>
                  </div>
                ) : null,
              )}
            </dl>
            {profile.files.length > 0 && (
              <ul className="flex flex-wrap gap-2">
                {profile.files.map((f) => (
                  <li key={f.document_id}>
                    <Link
                      href={`/documents/${f.document_id}`}
                      className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
                    >
                      <FileText className="size-3.5" aria-hidden />
                      {f.field === "cv" ? t("panel.cv") : f.field}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        {profile.screening ? (
          <section aria-labelledby={`${ids}-screen`} className="space-y-2 text-sm">
            <h3 id={`${ids}-screen`} className="eyebrow">
              {t("panel.screening")}
            </h3>
            <p>{profile.screening.overall ?? profile.screening.status}</p>
            <ul className="space-y-1">
              {profile.screening.criteria.map((c) => (
                <li key={c.criterion} className="flex justify-between gap-2">
                  <span>{c.criterion}</span>
                  <span className="text-muted-foreground">{c.result.replaceAll("_", " ")}</span>
                </li>
              ))}
            </ul>
          </section>
        ) : (
          <p className="text-xs text-muted-foreground">{t("panel.notScreened")}</p>
        )}

        <section aria-labelledby={`${ids}-timeline`} className="space-y-2">
          <h3 id={`${ids}-timeline`} className="eyebrow">
            {t("panel.timeline")}
          </h3>
          <ol className="relative space-y-3 border-s ps-4" data-timeline>
            {profile.timeline.map((event, index) => (
              <TimelineItem
                key={`${event.kind}-${event.at}-${index}`}
                event={event}
                format={format}
                t={t}
                stageLabel={stageLabel}
              />
            ))}
          </ol>
        </section>
      </div>
    </aside>
  );
});

function TimelineItem({
  event,
  format,
  t,
  stageLabel,
}: {
  event: HireEvent;
  format: ReturnType<typeof useFormatter>;
  t: ReturnType<typeof useTranslations<"Hire">>;
  stageLabel: (key: string) => string;
}) {
  const detail = event.detail as Record<string, unknown>;
  const text = (key: string): string | null => (typeof detail[key] === "string" ? (detail[key] as string) : null);
  const icon =
    event.kind === "note" ? (
      <MessageSquareText aria-hidden />
    ) : event.kind === "message" ? (
      <Send aria-hidden className="rtl:-scale-x-100" />
    ) : event.kind === "flag" ? (
      <Flag aria-hidden />
    ) : (
      <ArrowRight aria-hidden className="rtl:rotate-180" />
    );
  let title: string;
  switch (event.kind) {
    case "stage":
      title = t(detail.undo ? "timeline.undo" : "timeline.stage", {
        from: stageLabel(text("from") ?? ""),
        to: stageLabel(text("to") ?? ""),
      });
      break;
    case "message":
      title = event.title.startsWith("Acknowledgement")
        ? t("timeline.acknowledgement")
        : t("timeline.message", {
            status: t(`messageStatus.${text("status") ?? "sent"}` as "messageStatus.sent"),
          });
      break;
    case "note":
    case "application":
    case "flag":
    case "referral":
    case "screening":
      title = t(`timeline.${event.kind}`);
      break;
    default:
      title = event.title;
  }
  return (
    <li className="relative text-sm" data-event={event.kind}>
      <span className="absolute -start-[1.4rem] top-0.5 flex size-5 items-center justify-center rounded-full border bg-background text-muted-foreground [&_svg]:size-3">
        {icon}
      </span>
      <p className="font-medium">
        {title}
        {text("by") && <span className="font-normal text-muted-foreground"> · {text("by")}</span>}
      </p>
      {text("text") && <p className="whitespace-pre-wrap text-muted-foreground">{text("text")}</p>}
      {text("subject") && <p className="text-muted-foreground">{text("subject")}</p>}
      {Array.isArray(detail.reasons) && <p className="text-muted-foreground">{(detail.reasons as string[]).join(", ")}</p>}
      <time className="text-xs text-muted-foreground" dateTime={event.at}>
        {format.dateTime(new Date(event.at), { dateStyle: "medium", timeStyle: "short" })}
      </time>
    </li>
  );
}
