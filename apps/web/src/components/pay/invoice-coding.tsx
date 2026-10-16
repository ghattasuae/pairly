// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Loader2, Sparkles, Wand2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useRouter } from "@/i18n/navigation";
import { askModelCoding, codeInvoiceLines } from "@/lib/actions/pay";
import { acceptHigh, applySuggestion, changesOf, draftsOf, percent, type Drafts } from "@/lib/pay/coding";
import type { PayCodingEntry, PayCodingSuggestion, PayInvoiceCoding, PayModelLine } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";

function entryName(entries: readonly PayCodingEntry[], code: string | null | undefined, locale: string): string {
  const entry = entries.find((e) => e.code === code);
  if (!entry) return code ?? "";
  return locale === "ar" && entry.name_ar ? entry.name_ar : entry.name;
}

function SuggestionChip({
  suggestion,
  coding,
  locale,
  onUse,
  disabled,
}: {
  suggestion: PayCodingSuggestion;
  coding: PayInvoiceCoding;
  locale: string;
  onUse: () => void;
  disabled: boolean;
}) {
  const t = useTranslations("Pay.coding");
  return (
    <div className="space-y-1" data-suggestion={suggestion.method} data-suggestion-level={suggestion.level}>
      <p className="text-xs">
        <span className="font-mono" dir="ltr">
          {suggestion.gl_code}
        </span>{" "}
        <bdi>{entryName(coding.gl_accounts, suggestion.gl_code, locale)}</bdi>
        {suggestion.cost_centre && (
          <>
            {" · "}
            <span className="font-mono" dir="ltr">
              {suggestion.cost_centre}
            </span>
          </>
        )}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={suggestion.level === "high" ? "default" : suggestion.level === "medium" ? "signal" : "outline"}>
          {t(`level.${suggestion.level}`)}
          {suggestion.method === "history" ? ` ${t("percent", { value: percent(suggestion.confidence) })}` : ""}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {suggestion.method === "model" ? t("fromModel") : t("fromHistory", { count: suggestion.support })}
        </span>
        <Button type="button" size="sm" variant="outline" onClick={onUse} disabled={disabled}>
          {t("use")}
        </Button>
      </div>
      {suggestion.reason && <p className="text-xs text-muted-foreground">{suggestion.reason}</p>}
    </div>
  );
}

/**
 * GL coding of an invoice's lines (brief 4.5.3): each line's GL account and cost centre
 * from the lists in force, with the suggestion from earlier coding and its confidence. A
 * suggestion is applied only when the person uses it; an optional model suggestion is
 * shown, never applied. Saving runs the invoice checks again.
 */
export function InvoiceCoding({ initial, locale }: { initial: PayInvoiceCoding; locale: string }) {
  const t = useTranslations("Pay.coding");
  const router = useRouter();
  const id = useId();
  const [coding, setCoding] = useState(initial);
  const [drafts, setDrafts] = useState<Drafts>(() => draftsOf(initial.lines));
  const [model, setModel] = useState<Record<number, PayModelLine>>({});
  const [busy, setBusy] = useState<"save" | "model" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const editable = coding.can_code;
  const changes = changesOf(coding.lines, drafts);
  const hasList = coding.gl_accounts.length > 0;
  const highWaiting = coding.lines.some(
    (l) => l.suggestion?.level === "high" && !(drafts[l.line_index]?.gl_code ?? ""),
  );

  function set(index: number, patch: Partial<{ gl_code: string; cost_centre: string }>) {
    setDrafts((d) => ({ ...d, [index]: { gl_code: "", cost_centre: "", ...d[index], ...patch } }));
    setSaved(false);
  }

  function use(index: number, suggestion: PayCodingSuggestion) {
    setDrafts((d) => ({ ...d, [index]: applySuggestion(d[index] ?? { gl_code: "", cost_centre: "" }, suggestion) }));
    setSaved(false);
  }

  async function save() {
    if (changes.length === 0) return;
    setBusy("save");
    setError(null);
    const result = await codeInvoiceLines(coding.invoice_id, changes);
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setCoding(result.coding);
    setDrafts(draftsOf(result.coding.lines));
    setSaved(true);
    router.refresh();
  }

  async function ask() {
    setBusy("model");
    setError(null);
    const wanted = coding.lines.filter((l) => l.suggestion?.level !== "high").map((l) => l.line_index);
    const result = await askModelCoding(coding.invoice_id, wanted);
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setModel(Object.fromEntries(result.lines.map((l) => [l.line_index, l])));
  }

  return (
    <Panel title={t("title")} data-invoice-coding>
      <p className="text-sm text-muted-foreground">{t("intro")}</p>
      {!hasList ? (
        <p className="text-sm" data-coding-no-list>
          {t("noList")}
        </p>
      ) : coding.lines.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("noLines")}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr className="border-b">
                <th scope="col" className="px-2 py-2 text-start font-medium">{t("col.line")}</th>
                <th scope="col" className="px-2 py-2 text-start font-medium">{t("col.gl")}</th>
                <th scope="col" className="px-2 py-2 text-start font-medium">{t("col.costCentre")}</th>
                <th scope="col" className="px-2 py-2 text-start font-medium">{t("col.suggestion")}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {coding.lines.map((line) => {
                const draft = drafts[line.line_index] ?? { gl_code: "", cost_centre: "" };
                const fromModel = model[line.line_index];
                return (
                  <tr key={line.line_index} data-coding-line={line.line_index} className="align-top">
                    <td className="px-2 py-2">
                      <span className="text-muted-foreground tabular-nums">{line.line_index + 1}.</span>{" "}
                      <bdi>{line.description ?? t("noDescription")}</bdi>
                      {line.source === "auto" && (
                        <Badge variant="outline" className="ms-2" data-coding-auto>
                          {t("auto")}
                        </Badge>
                      )}
                    </td>
                    <td className="px-2 py-2">
                      <select
                        aria-label={t("glFor", { line: line.line_index + 1 })}
                        id={`${id}-gl-${line.line_index}`}
                        className="h-9 w-full min-w-40 rounded-md border bg-background px-2 text-sm"
                        value={draft.gl_code}
                        disabled={!editable || busy !== null}
                        onChange={(e) => set(line.line_index, { gl_code: e.target.value })}
                      >
                        <option value="">{t("uncoded")}</option>
                        {coding.gl_accounts.map((e) => (
                          <option key={e.code} value={e.code}>
                            {e.code} {locale === "ar" && e.name_ar ? e.name_ar : e.name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="px-2 py-2">
                      <select
                        aria-label={t("ccFor", { line: line.line_index + 1 })}
                        className="h-9 w-full min-w-32 rounded-md border bg-background px-2 text-sm"
                        value={draft.cost_centre}
                        disabled={!editable || busy !== null || coding.cost_centres.length === 0}
                        onChange={(e) => set(line.line_index, { cost_centre: e.target.value })}
                      >
                        <option value="">{t("uncoded")}</option>
                        {coding.cost_centres.map((e) => (
                          <option key={e.code} value={e.code}>
                            {e.code} {locale === "ar" && e.name_ar ? e.name_ar : e.name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="space-y-2 px-2 py-2">
                      {line.suggestion ? (
                        <SuggestionChip
                          suggestion={line.suggestion}
                          coding={coding}
                          locale={locale}
                          onUse={() => line.suggestion && use(line.line_index, line.suggestion)}
                          disabled={!editable || busy !== null}
                        />
                      ) : (
                        !fromModel && <span className="text-xs text-muted-foreground">{t("noSuggestion")}</span>
                      )}
                      {fromModel?.suggestion && (
                        <SuggestionChip
                          suggestion={fromModel.suggestion}
                          coding={coding}
                          locale={locale}
                          onUse={() => fromModel.suggestion && use(line.line_index, fromModel.suggestion)}
                          disabled={!editable || busy !== null}
                        />
                      )}
                      {fromModel && !fromModel.suggestion && (
                        <span className="text-xs text-muted-foreground" data-model-problem={fromModel.problem ?? ""}>
                          {fromModel.problem === "model_unavailable" ? t("modelUnavailable") : t("modelNothing")}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {coding.auto_coding && <p className="text-xs text-muted-foreground">{t("autoOn")}</p>}
      {editable && hasList && coding.lines.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" size="sm" variant="outline" disabled={!highWaiting || busy !== null} onClick={() => setDrafts(acceptHigh(coding.lines, drafts))}>
            <Wand2 aria-hidden />
            {t("useHigh")}
          </Button>
          <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={ask}>
            {busy === "model" ? <Loader2 aria-hidden className="animate-spin" /> : <Sparkles aria-hidden />}
            {t("askModel")}
          </Button>
          <Button type="button" size="sm" disabled={changes.length === 0 || busy !== null} onClick={save}>
            {busy === "save" && <Loader2 aria-hidden className="animate-spin" />}
            {t("save", { count: changes.length })}
          </Button>
          {saved && (
            <span role="status" className="inline-flex items-center gap-1 text-sm text-muted-foreground">
              <Check aria-hidden className="size-4" />
              {t("saved")}
            </span>
          )}
        </div>
      )}
      <ErrorText code={error} />
    </Panel>
  );
}
