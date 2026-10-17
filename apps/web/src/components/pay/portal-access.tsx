// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { KeyRound, Loader2, UserX } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form-controls";
import { givePortalAccess, revokePortalAccess } from "@/lib/actions/pay";
import type { PayPortalAccess, PayPortalAccessList } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";

export interface PortalAccessActions {
  give: typeof givePortalAccess;
  revoke: typeof revokePortalAccess;
}

const DEFAULT_ACTIONS: PortalAccessActions = { give: givePortalAccess, revoke: revokePortalAccess };

/**
 * Who at the vendor may sign in to the vendor portal (brief 4.7). Only a contact on file
 * can get access; the sign-in link goes to that address by email and is never shown here.
 */
export function PortalAccessPanel({
  vendorId,
  contacts,
  initial,
  canWrite,
  actions = DEFAULT_ACTIONS,
}: {
  vendorId: string;
  contacts: Array<{ name: string; email?: string | null }>;
  initial: PayPortalAccessList;
  canWrite: boolean;
  actions?: PortalAccessActions;
}) {
  const t = useTranslations("Pay.portal");
  const format = useFormatter();
  const ids = useId();
  const locale = useLocale() === "ar" ? "ar" : "en";
  const [items, setItems] = useState<PayPortalAccess[]>(initial.items);
  const [email, setEmail] = useState("");
  const [lang, setLang] = useState<"en" | "ar">(locale);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const withEmail = contacts.filter((c) => c.email);

  const give = async () => {
    if (!email) return;
    setBusy("give");
    setError(null);
    const result = await actions.give(vendorId, { email, locale: lang });
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setItems((list) => [result.access, ...list.filter((i) => i.id !== result.access.id)]);
    setNotice(t("sent", { email: result.access.email }));
    setEmail("");
  };

  const revoke = async (access: PayPortalAccess) => {
    setBusy(access.id);
    setError(null);
    const result = await actions.revoke(vendorId, access.id);
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setItems((list) => list.map((i) => (i.id === access.id ? result.access : i)));
    setNotice(t("revoked", { email: access.email }));
  };

  return (
    <Panel title={t("title")} data-portal-access>
      <p className="text-sm text-muted-foreground">
        {t("intro")} <span dir="ltr" className="font-mono text-xs">{initial.portal_path}</span>
      </p>
      <p role="status" aria-live="polite" className="text-sm">
        {notice ?? ""}
      </p>
      <ErrorText code={error} />
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("none")}</p>
      ) : (
        <ul className="divide-y rounded-lg border">
          {items.map((a) => (
            <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm" data-portal-contact={a.status}>
              <span className="min-w-0">
                <span className="block font-medium">{a.name}</span>
                <span className="block text-xs text-muted-foreground" dir="ltr">
                  {a.email}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {a.last_sign_in_at
                    ? t("lastSignIn", { date: format.dateTime(new Date(a.last_sign_in_at), { dateStyle: "medium" }) })
                    : t("neverSignedIn")}
                </span>
              </span>
              {a.status === "active" ? (
                canWrite && (
                  <Button type="button" variant="ghost" size="sm" disabled={busy !== null} onClick={() => void revoke(a)}>
                    {busy === a.id ? <Loader2 className="animate-spin" aria-hidden /> : <UserX aria-hidden />}
                    {t("revoke")}
                  </Button>
                )
              ) : (
                <Badge variant="outline">{t("revokedBadge")}</Badge>
              )}
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void give();
          }}
        >
          <div className="min-w-48 flex-1 space-y-1">
            <label htmlFor={`${ids}-email`} className="block text-sm font-medium">
              {t("contact")}
            </label>
            <Select id={`${ids}-email`} value={email} onChange={(e) => setEmail(e.target.value)}>
              <option value="">{t("choose")}</option>
              {withEmail.map((c) => (
                <option key={c.email ?? ""} value={c.email ?? ""}>
                  {c.name} ({c.email})
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <label htmlFor={`${ids}-lang`} className="block text-sm font-medium">
              {t("language")}
            </label>
            <Select id={`${ids}-lang`} value={lang} onChange={(e) => setLang(e.target.value === "ar" ? "ar" : "en")}>
              <option value="en">English</option>
              <option value="ar">العربية</option>
            </Select>
          </div>
          <Button type="submit" disabled={busy !== null || !email}>
            {busy === "give" ? <Loader2 className="animate-spin" aria-hidden /> : <KeyRound aria-hidden />}
            {t("give")}
          </Button>
        </form>
      )}
    </Panel>
  );
}
