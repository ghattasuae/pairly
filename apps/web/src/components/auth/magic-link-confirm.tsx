// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { useLocale, useTranslations } from "next-intl";
import { useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { safeNextPath } from "@/lib/safe-path";

import { AuthCard, FormError } from "./auth-card";

const subscribe = () => () => {};

/** Reads the token from the URL fragment (it never reaches the server or logs). */
export function readTokenFromHash(hash: string): string | null {
  const token = new URLSearchParams(hash.replace(/^#/, "")).get("token");
  return token && /^[A-Za-z0-9_-]{16,256}$/.test(token) ? token : null;
}

/** Where to land after signing in: a same-site page from the fragment, or home. */
export function readNextFromHash(hash: string): string | null {
  return safeNextPath(new URLSearchParams(hash.replace(/^#/, "")).get("next"));
}

export function verifyHref(token: string, locale: string, next?: string | null): string {
  const params = new URLSearchParams({
    token,
    callbackURL: safeNextPath(next) ?? `/${locale}`,
    errorCallbackURL: `/${locale}/sign-in`,
  });
  return `/api/auth/magic-link/verify?${params.toString()}`;
}

/**
 * Landing page for the emailed link. Mail scanners open links but do not click buttons,
 * so the single-use token is only spent when the person selects "Sign in".
 */
export function MagicLinkConfirm() {
  const t = useTranslations("Confirm");
  const auth = useTranslations("Auth");
  const locale = useLocale();
  const token = useSyncExternalStore(
    subscribe,
    () => readTokenFromHash(window.location.hash),
    () => undefined,
  );
  const next = useSyncExternalStore(
    subscribe,
    () => readNextFromHash(window.location.hash),
    () => null,
  );

  return (
    <AuthCard title={t("title")} subtitle={token ? t("body") : undefined}>
      {token === undefined ? null : token ? (
        <div className="space-y-4">
          <Button asChild size="lg" className="w-full">
            <a href={verifyHref(token, locale, next)} rel="noreferrer">
              {t("button")}
            </a>
          </Button>
          <p className="text-xs text-muted-foreground">{t("why")}</p>
        </div>
      ) : (
        <div className="space-y-4">
          <FormError id="confirm-error">{t("missing")}</FormError>
          <Link href="/sign-in" className="text-sm font-medium text-primary underline-offset-4 hover:underline">
            {auth("submit")}
          </Link>
        </div>
      )}
    </AuthCard>
  );
}
