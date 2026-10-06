// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowRight, MailCheck } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { AuthCard, FormError } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { Link, useRouter } from "@/i18n/navigation";
import { acceptInvitation, prepareInviteSignIn } from "@/lib/actions/invitations";
import { authClient } from "@/lib/auth/client";
import { isolate } from "@/lib/bidi";

export interface InvitePreview {
  org_name: string;
  email: string;
  role: string;
  workflow: string | null;
  expires_at: string;
}

export type InviteView =
  | { kind: "invalid" }
  | { kind: "unavailable" }
  /** Not signed in: send a magic link to the invited address, then come back here. */
  | { kind: "signIn"; preview: InvitePreview }
  /** Signed in as the invited address. */
  | { kind: "accept"; preview: InvitePreview; currentEmail: string }
  /** Signed in as somebody else. */
  | { kind: "wrongUser"; preview: InvitePreview; currentEmail: string };

type Result<T extends object = object> = ({ ok: true } & T) | { ok: false; code: string };

export interface InvitePanelProps {
  token: string;
  view: InviteView;
  /** Overrides for stories and tests. Default to the server actions and Better Auth. */
  prepare?: (token: string) => Promise<Result<{ email: string }>>;
  sendLink?: (email: string, locale: string, callbackURL: string) => Promise<{ ok: boolean; status?: number }>;
  accept?: (token: string) => Promise<Result<{ orgName: string }>>;
  signOut?: () => Promise<void>;
}

const KNOWN_ERRORS = new Set([
  "invitation_invalid",
  "invitation_email_mismatch",
  "email_not_verified",
  "not_signed_in",
  "rateLimited",
]);

async function defaultSendLink(email: string, locale: string, callbackURL: string) {
  const { error } = await authClient.signIn.magicLink({
    email,
    callbackURL,
    errorCallbackURL: `/${locale}/sign-in`,
    metadata: { locale },
  } as Parameters<typeof authClient.signIn.magicLink>[0]);
  return error ? { ok: false, status: error.status } : { ok: true };
}

function Summary({ preview }: { preview: InvitePreview }) {
  const t = useTranslations("Invite");
  const roles = useTranslations("Roles");
  const workflows = useTranslations("Workflows");
  const format = useFormatter();
  const role = roles.has(preview.role as "admin") ? roles(preview.role as "admin") : preview.role;
  return (
    <div className="space-y-2 rounded-xl border bg-surface p-4 text-sm">
      <p>{t("body", { org: preview.org_name, role })}</p>
      {preview.workflow && (
        <p className="text-muted-foreground">
          {t("scope", {
            workflow: workflows.has(preview.workflow as "pay") ? workflows(preview.workflow as "pay") : preview.workflow,
          })}
        </p>
      )}
      <p className="text-muted-foreground">{t("forEmail", { email: isolate(preview.email) })}</p>
      <p className="text-muted-foreground">
        {t("expires", { date: format.dateTime(new Date(preview.expires_at), { dateStyle: "long" }) })}
      </p>
    </div>
  );
}

/** The invite page: sign in as the invited address if needed, then accept. */
export function InvitePanel({
  token,
  view,
  prepare = prepareInviteSignIn,
  sendLink = defaultSendLink,
  accept = acceptInvitation,
  signOut,
}: InvitePanelProps) {
  const t = useTranslations("Invite");
  const locale = useLocale();
  const router = useRouter();
  const errorId = useId();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const showError = (code: string) => setError(KNOWN_ERRORS.has(code) ? code : "generic");

  if (view.kind === "invalid" || view.kind === "unavailable") {
    return (
      <AuthCard title={t("invalidTitle")} subtitle={view.kind === "invalid" ? t("invalid") : t("errors.generic")}>
        <Button asChild variant="outline">
          <Link href="/">{t("goHome")}</Link>
        </Button>
      </AuthCard>
    );
  }

  const { preview } = view;
  const callbackURL = `/${locale}/invite/${token}`;

  const startSignIn = async () => {
    setPending(true);
    setError(null);
    try {
      const prepared = await prepare(token);
      if (!prepared.ok) return showError(prepared.code);
      const sent = await sendLink(prepared.email, locale, callbackURL);
      if (!sent.ok) return showError(sent.status === 429 ? "rateLimited" : "generic");
      try {
        sessionStorage.setItem("pairly.signInEmail", prepared.email);
      } catch {
        /* storage can be blocked; the next page has a generic message */
      }
      router.push("/check-email");
    } catch {
      showError("generic");
    } finally {
      setPending(false);
    }
  };

  const join = async () => {
    setPending(true);
    setError(null);
    try {
      const result = await accept(token);
      if (!result.ok) {
        showError(result.code);
        setPending(false);
        return;
      }
      // The session now points at the new organisation: render home from the server.
      router.replace("/");
      router.refresh();
    } catch {
      showError("generic");
      setPending(false);
    }
  };

  const switchUser = async () => {
    setPending(true);
    if (signOut) await signOut();
    else await authClient.signOut();
    window.location.reload();
  };

  return (
    <AuthCard
      title={view.kind === "wrongUser" ? t("wrongUserTitle") : t("title", { org: preview.org_name })}
    >
      <Summary preview={preview} />
      {error && <FormError id={errorId}>{t(`errors.${error}` as "errors.generic")}</FormError>}

      {view.kind === "signIn" && (
        <div className="space-y-3">
          <Button size="lg" className="w-full" onClick={startSignIn} disabled={pending} aria-describedby={error ? errorId : undefined}>
            {pending ? t("sending") : t("signIn")}
            {!pending && <MailCheck aria-hidden />}
          </Button>
          <p className="text-xs text-muted-foreground">{t("signInHint")}</p>
        </div>
      )}

      {view.kind === "accept" && (
        <div className="space-y-3">
          <Button size="lg" className="w-full" onClick={join} disabled={pending} aria-describedby={error ? errorId : undefined}>
            {pending ? t("accepting") : t("accept")}
            {!pending && <ArrowRight className="rtl:rotate-180" aria-hidden />}
          </Button>
          <p className="text-xs text-muted-foreground">{t("acceptHint", { email: isolate(view.currentEmail) })}</p>
        </div>
      )}

      {view.kind === "wrongUser" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {t("wrongUser", { current: isolate(view.currentEmail), email: isolate(preview.email) })}
          </p>
          <Button size="lg" variant="outline" className="w-full" onClick={switchUser} disabled={pending}>
            {t("signOut")}
          </Button>
        </div>
      )}
    </AuthCard>
  );
}
