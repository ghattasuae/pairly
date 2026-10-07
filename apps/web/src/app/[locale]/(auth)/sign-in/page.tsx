// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { getTranslations, setRequestLocale } from "next-intl/server";

import { AuthCard, FormError } from "@/components/auth/auth-card";
import { SignInForm } from "@/components/auth/sign-in-form";
import { redirect } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { getSession } from "@/lib/auth/server";
import { ssoAvailability } from "@/lib/env";
import { safeNextPath } from "@/lib/safe-path";

const KNOWN_ERRORS = new Set(["INVALID_TOKEN", "EXPIRED_TOKEN", "new_user_signup_disabled", "signInRequired"]);

interface Props {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ error?: string | string[]; next?: string | string[] }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const locale = toLocale((await params).locale);
  const t = await getTranslations({ locale, namespace: "Auth" });
  return { title: t("signInTitle") };
}

export default async function SignInPage({ params, searchParams }: Props) {
  const locale = toLocale((await params).locale);
  setRequestLocale(locale);
  const query = await searchParams;
  // Only a plain same-site path for this locale (for example an inbox item after a fresh
  // two-factor sign-in). Anything else is ignored.
  const nextPath = safeNextPath(query.next);
  const next = nextPath?.startsWith(`/${locale}/`) ? nextPath : null;
  if (await getSession()) redirect({ href: next ? next.slice(locale.length + 1) : "/", locale });

  const t = await getTranslations("Auth");
  const raw = query.error;
  const code = typeof raw === "string" ? raw : undefined;
  const errorKey = code && KNOWN_ERRORS.has(code) ? code : code ? "generic" : undefined;

  return (
    <AuthCard title={t("signInTitle")} subtitle={t("signInSubtitle")}>
      {errorKey && <FormError id="sign-in-link-error">{t(`errors.${errorKey}` as "errors.generic")}</FormError>}
      <SignInForm sso={ssoAvailability()} next={next} />
      <p className="text-xs text-muted-foreground">{t("footnote")}</p>
    </AuthCard>
  );
}
