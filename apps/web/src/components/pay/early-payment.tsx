// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { BadgePercent } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import type { PayEarlyPayment } from "@/lib/pay/types";

function money(currency: string, amount: string | number): string {
  return `${currency} ${String(amount)}`;
}

/**
 * The early-payment discount on an open invoice (brief 4.6.4): what paying by the
 * pay-by date saves, from the invoice's printed terms or the vendor's agreed terms.
 * Amounts are shown as the API computed them, at the currency's minor unit.
 */
export function EarlyPaymentNote({ offer }: { offer: PayEarlyPayment | null | undefined }) {
  const t = useTranslations("Pay.invoice.early");
  const format = useFormatter();
  if (!offer) return null;
  const when = format.dateTime(new Date(`${offer.pay_by}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" });
  return (
    <section
      className={
        offer.available
          ? "flex gap-3 rounded-lg border border-primary/40 bg-primary/5 p-4 text-sm"
          : "flex gap-3 rounded-lg border p-4 text-sm text-muted-foreground"
      }
      data-early-payment={offer.available ? "available" : "expired"}
    >
      <BadgePercent aria-hidden className="mt-0.5 size-5 shrink-0" />
      <div className="space-y-1">
        <h2 className="font-medium">{t("title")}</h2>
        <p>
          {t("body", {
            terms: offer.terms,
            pay: money(offer.currency, offer.pay_early),
            when,
            amount: money(offer.currency, offer.amount),
            saving: money(offer.currency, offer.saving),
          })}
        </p>
        <p className="text-xs text-muted-foreground">
          {t(`source.${offer.source}`)}
          {offer.annual_rate !== null && offer.annual_rate !== undefined
            ? ` · ${t("rate", { rate: format.number(Number(offer.annual_rate), { style: "percent", maximumFractionDigits: 1 }) })}`
            : ""}
        </p>
        {offer.available ? (
          <p className="text-xs" data-days-left={offer.days_left}>
            {t("daysLeft", { count: offer.days_left })} · {t("inRun")}
          </p>
        ) : (
          <p className="text-xs">{t("expired", { when })}</p>
        )}
      </div>
    </section>
  );
}
