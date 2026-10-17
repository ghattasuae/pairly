// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Copy, Loader2, Send } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Link } from "@/i18n/navigation";
import { createInvite, revokeInvite } from "@/lib/actions/pay";
import type { PayInvite } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";

/**
 * Invite a vendor to fill in its own details (brief 4.1.1). The link is signed and
 * single use; it is shown once, here, for you to send. Its token sits after `#`, so it
 * never reaches a server log.
 */
export function InvitesView({ initial }: { initial: PayInvite[] }) {
  const t = useTranslations("Pay.invites");
  const format = useFormatter();
  const ids = useId();
  const [items, setItems] = useState(initial);
  const [company, setCompany] = useState("");
  const [email, setEmail] = useState("");
  const [locale, setLocale] = useState<"en" | "ar">("en");
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy("create");
    setError(null);
    setCopied(false);
    const result = await createInvite({ company_hint: company, email, locale });
    setBusy(null);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    const { token: _token, path, ...invite } = result.invite;
    setLink(`${window.location.origin}${path}`);
    setItems((list) => [invite, ...list]);
    setCompany("");
    setEmail("");
  }

  async function revoke(id: string) {
    setBusy(id);
    const result = await revokeInvite(id);
    setBusy(null);
    if (result.ok) setItems((list) => list.map((i) => (i.id === id ? result.invite : i)));
    else setError(result.code);
  }

  async function copy() {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="space-y-6">
      <Panel title={t("new")} data-invite-form>
        <form onSubmit={create} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor={`${ids}-company`}>{t("company")}</Label>
              <Input id={`${ids}-company`} maxLength={200} value={company} onChange={(e) => setCompany(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${ids}-email`}>{t("email")}</Label>
              <Input id={`${ids}-email`} type="email" dir="ltr" maxLength={254} value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${ids}-locale`}>{t("language")}</Label>
              <Select id={`${ids}-locale`} value={locale} onChange={(e) => setLocale(e.target.value === "ar" ? "ar" : "en")}>
                <option value="en">{t("languages.en")}</option>
                <option value="ar">{t("languages.ar")}</option>
              </Select>
            </div>
          </div>
          <ErrorText code={error} />
          <Button type="submit" disabled={busy !== null} data-create-invite>
            {busy === "create" ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
            {t("create")}
          </Button>
        </form>
        {link && (
          <div className="space-y-2 rounded-lg border bg-background p-3" data-invite-link>
            <p className="text-sm font-medium">{t("linkOnce")}</p>
            <p className="break-all font-mono text-xs" dir="ltr">
              {link}
            </p>
            <Button size="sm" variant="outline" onClick={() => void copy()}>
              <Copy aria-hidden />
              {copied ? t("copied") : t("copy")}
            </Button>
          </div>
        )}
      </Panel>
      <Panel title={t("list")} data-invite-list>
        {items.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("empty")}</p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {items.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm" data-invite={i.state}>
                <span className="font-medium">{i.company_hint || t("noCompany")}</span>
                {i.email && (
                  <bdi dir="ltr" className="text-muted-foreground">
                    {i.email}
                  </bdi>
                )}
                <Badge variant={i.state === "open" ? "signal" : "outline"}>{t(`state.${i.state}` as "state.open")}</Badge>
                <span className="text-xs text-muted-foreground">
                  {t("expires", { when: format.dateTime(new Date(i.expires_at), { dateStyle: "medium" }) })}
                </span>
                <span className="ms-auto flex gap-2">
                  {i.vendor_id && (
                    <Link href={`/pay/vendors/${i.vendor_id}`} className="inline-flex h-8 items-center rounded-md border px-3 text-[0.8rem] hover:bg-accent">
                      {t("review")}
                    </Link>
                  )}
                  {(i.state === "open" || i.state === "opened") && (
                    <Button size="sm" variant="outline" disabled={busy === i.id} onClick={() => void revoke(i.id)} data-revoke>
                      {t("revoke")}
                    </Button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
