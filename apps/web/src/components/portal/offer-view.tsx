// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Download, Loader2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox, Textarea } from "@/components/ui/form-controls";
import { errorKey } from "@/lib/action-result";
import { answerOffer } from "@/lib/actions/portal-offer";
import type { HirePortalOffer } from "@/lib/hire/types";

const MONEY = ["base_monthly", "housing_monthly", "transport_monthly", "other_monthly"] as const;

/**
 * The candidate's offer in the portal (M3): the letter's content, a PDF download, and
 * accept or decline, confirmed, once. There is no e-signature: this answer is recorded
 * as the candidate's decision.
 */
export function OfferView({ initial }: { initial: HirePortalOffer }) {
  const t = useTranslations("PortalOffer");
  const tOffers = useTranslations("Offers");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const ids = useId();
  const [offer, setOffer] = useState(initial);
  const [confirmed, setConfirmed] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"accept" | "decline" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const comp = offer.compensation;
  const money = (value: string) => `${comp.currency} ${Number(value).toLocaleString("en", { minimumFractionDigits: 2 })}`;

  async function answer(accept: boolean) {
    setBusy(accept ? "accept" : "decline");
    setError(null);
    const result = await answerOffer({ offerId: offer.id, accept, note });
    setBusy(null);
    if (result.ok) setOffer(result.offer);
    else setError(result.code);
  }

  return (
    <div className="space-y-6" data-portal-offer={offer.status}>
      <header className="space-y-1">
        <p className="text-sm text-muted-foreground">
          {offer.company} · {offer.reference}
        </p>
        <h1 className="font-display text-2xl">{t("title", { job: offer.job_title })}</h1>
        <p className="text-sm text-muted-foreground">
          {t("starts", { date: format.dateTime(new Date(`${offer.start_date}T00:00:00`), { dateStyle: "long" }) })} · {offer.template_name}
        </p>
      </header>
      <section className="space-y-2 rounded-xl border bg-surface p-5">
        <h2 className="font-display text-lg">{tOffers("compensation")}</h2>
        <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
          {MONEY.filter((k) => Number(comp[k]) > 0).map((k) => (
            <div key={k} className="flex justify-between gap-2 sm:block">
              <dt className="text-muted-foreground">{tOffers(`field.${k}`)}</dt>
              <dd dir="ltr">{money(comp[k])}</dd>
            </div>
          ))}
          {Number(comp.bonus_target_pct) > 0 && (
            <div className="flex justify-between gap-2 sm:block">
              <dt className="text-muted-foreground">{tOffers("field.bonus_target_pct")}</dt>
              <dd dir="ltr">{Number(comp.bonus_target_pct)}%</dd>
            </div>
          )}
          <div className="flex justify-between gap-2 font-medium sm:block">
            <dt>{tOffers("field.annual_total")}</dt>
            <dd dir="ltr" data-offer-annual>
              {money(comp.annual_total)}
            </dd>
          </div>
        </dl>
      </section>
      {offer.conditions.length > 0 && (
        <section className="space-y-2 rounded-xl border bg-surface p-5">
          <h2 className="font-display text-lg">{tOffers("conditions")}</h2>
          <ol className="list-decimal space-y-1 ps-5 text-sm">
            {offer.conditions.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ol>
        </section>
      )}
      <Button asChild variant="outline">
        <a href={`/api/portal/offers/${offer.id}/pdf`} data-portal-offer-pdf>
          <Download aria-hidden />
          {t("download")}
        </a>
      </Button>
      {offer.status === "sent" ? (
        <section className="space-y-3 rounded-xl border bg-surface p-5" data-offer-answer>
          <h2 className="font-display text-lg">{t("answerTitle")}</h2>
          <p className="text-sm text-muted-foreground">{t("answerHint")}</p>
          <label htmlFor={`${ids}-note`} className="text-sm font-medium">
            {t("note")}
          </label>
          <Textarea id={`${ids}-note`} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
          <label className="flex items-start gap-2 text-sm">
            <Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} data-confirm-offer />
            <span>{t("confirm")}</span>
          </label>
          {error && (
            <p role="alert" className="text-sm text-danger-ink">
              {tErrors(errorKey(error) as "generic")}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button disabled={!confirmed || busy !== null} onClick={() => void answer(true)} data-accept-offer>
              {busy === "accept" && <Loader2 aria-hidden className="animate-spin" />}
              {t("accept")}
            </Button>
            <Button variant="outline" disabled={!confirmed || busy !== null} onClick={() => void answer(false)} data-decline-offer>
              {busy === "decline" && <Loader2 aria-hidden className="animate-spin" />}
              {t("decline")}
            </Button>
          </div>
        </section>
      ) : (
        <p role="status" className="flex items-center gap-2 rounded-xl border bg-surface p-5 font-medium" data-offer-answered>
          <Check aria-hidden className="size-5 text-primary" />
          {offer.status === "accepted" ? t("accepted") : t("declined")}
        </p>
      )}
    </div>
  );
}
