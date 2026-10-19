// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2, MailCheck } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { requestPortalLink } from "@/lib/actions/portal";

/**
 * Ask for a magic link to the candidate portal (no password). The organisation comes from
 * the careers page (`?org=<slug>`) or from an expired link (its organisation id). The
 * answer is the same whether or not anyone applied with the address.
 */
export function PortalSignIn({
  org,
  expired = false,
  request = requestPortalLink,
}: {
  org: string | null;
  expired?: boolean;
  request?: typeof requestPortalLink;
}) {
  const t = useTranslations("Portal");
  const locale = useLocale() === "ar" ? "ar" : "en";
  const ids = useId();
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [code, setCode] = useState<string | null>(null);

  const submit = async () => {
    if (!org || !email.trim()) return;
    setState("sending");
    const result = await request({ org, email: email.trim(), locale });
    if (result.ok) setState("sent");
    else {
      setCode(result.code);
      setState("error");
    }
  };

  return (
    <section aria-labelledby={`${ids}-title`} className="mx-auto max-w-md space-y-5" data-portal-sign-in>
      <div className="space-y-2">
        <p className="eyebrow">{t("eyebrow")}</p>
        <h1 id={`${ids}-title`} className="font-display text-3xl leading-tight">
          {expired ? t("signIn.expiredTitle") : t("signIn.title")}
        </h1>
        <p className="text-muted-foreground">{expired ? t("signIn.expired") : t("signIn.intro")}</p>
      </div>
      {!org ? (
        <p className="rounded-xl border bg-surface p-4 text-sm">{t("signIn.needOrg")}</p>
      ) : state === "sent" ? (
        <p role="status" className="flex items-start gap-2 rounded-xl border bg-surface p-4 text-sm" data-link-sent>
          <MailCheck className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
          {t("signIn.sent")}
        </p>
      ) : (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label htmlFor={`${ids}-email`} className="block text-sm font-medium">
            {t("signIn.email")}
          </label>
          <Input
            id={`${ids}-email`}
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            dir="ltr"
          />
          {state === "error" && (
            <p role="alert" className="text-sm text-danger-ink">
              {t(`errors.${code === "rate_limited" || code === "invalid_input" ? code : "generic"}`)}
            </p>
          )}
          <Button type="submit" className="w-full" disabled={state === "sending"}>
            {state === "sending" && <Loader2 className="animate-spin" aria-hidden />}
            {state === "sending" ? t("signIn.sending") : t("signIn.send")}
          </Button>
        </form>
      )}
    </section>
  );
}
