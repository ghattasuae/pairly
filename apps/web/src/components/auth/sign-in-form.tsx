// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowRight } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useRouter } from "@/i18n/navigation";
import { authClient } from "@/lib/auth/client";
import { errorKeyFor, type SignInErrorKey as ErrorKey } from "@/lib/auth/sign-in-errors";
import { isolate } from "@/lib/bidi";

import { FormError } from "./auth-card";

const emailSchema = z.email().max(254);

export interface SignInFormProps {
  sso: { google: boolean; microsoft: boolean };
  /** Override for stories and tests. Defaults to Better Auth. */
  sendLink?: (
    email: string,
    locale: string,
    next?: string | null,
  ) => Promise<{ ok: boolean; status?: number | undefined; code?: string | undefined }>;
  /** A same-site page to land on after signing in (validated by the page). Default: home. */
  next?: string | null;
}

async function defaultSendLink(email: string, locale: string, next?: string | null) {
  const { error } = await authClient.signIn.magicLink({
    email,
    callbackURL: next ?? `/${locale}`,
    errorCallbackURL: `/${locale}/sign-in`,
    // Picks the language of the email. Validated against the locale list on the server.
    metadata: { locale },
  } as Parameters<typeof authClient.signIn.magicLink>[0]);
  return error ? { ok: false, status: error.status, code: error.code } : { ok: true };
}

export function SignInForm({ sso, sendLink = defaultSendLink, next = null }: SignInFormProps) {
  const t = useTranslations("Auth");
  const locale = useLocale();
  const router = useRouter();
  const emailId = useId();
  const errorId = useId();
  const [email, setEmail] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ErrorKey | null>(null);

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const parsed = emailSchema.safeParse(email.trim());
    if (!parsed.success) {
      setError("invalidEmail");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const result = await sendLink(parsed.data, locale, next);
      if (result.ok) {
        // The address stays in this tab only (session storage), never in the URL.
        try {
          sessionStorage.setItem("pairly.signInEmail", parsed.data);
        } catch {
          /* storage can be blocked; the next page has a generic message */
        }
        router.push("/check-email");
        return;
      }
      setError(errorKeyFor(result));
    } catch {
      setError("generic");
    }
    setPending(false);
  };

  const social = (provider: "google" | "microsoft") => () =>
    void authClient.signIn.social({ provider, callbackURL: next ?? `/${locale}`, errorCallbackURL: `/${locale}/sign-in` });

  return (
    <div className="space-y-5">
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor={emailId}>{t("emailLabel")}</Label>
          <Input
            id={emailId}
            type="email"
            name="email"
            dir="ltr"
            autoComplete="email"
            inputMode="email"
            autoFocus
            required
            placeholder={t("emailPlaceholder")}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            aria-invalid={error === "invalidEmail" || undefined}
            aria-describedby={error ? errorId : undefined}
            className="text-start"
          />
        </div>
        {error && (
          <FormError id={errorId}>
            {error === "originMismatch"
              ? t("errors.originMismatch", { origin: isolate(window.location.origin) })
              : t(`errors.${error}`)}
          </FormError>
        )}
        <Button type="submit" size="lg" className="w-full" disabled={pending}>
          {pending ? t("sending") : t("submit")}
          {!pending && <ArrowRight className="rtl:rotate-180" aria-hidden />}
        </Button>
      </form>

      {(sso.google || sso.microsoft) && (
        <>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="h-px flex-1 bg-border" />
            {t("or")}
            <span className="h-px flex-1 bg-border" />
          </div>
          <div className="grid gap-2">
            {sso.google && (
              <Button variant="outline" size="lg" onClick={social("google")}>
                {t("google")}
              </Button>
            )}
            {sso.microsoft && (
              <Button variant="outline" size="lg" onClick={social("microsoft")}>
                {t("microsoft")}
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
