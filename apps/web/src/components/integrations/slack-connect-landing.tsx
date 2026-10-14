// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { CircleCheck, Link2Off, Loader2, MessageSquare } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { SignInForm } from "@/components/auth/sign-in-form";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { confirmSlackConnect } from "@/lib/actions/slack";
import { isolate } from "@/lib/bidi";
import { forgetSlackToken, loadSlackToken, readSlackToken, saveSlackToken } from "@/lib/slack-connect-token";

type View = "loading" | "signedOut" | "ready" | "connecting" | "done" | "invalid" | "unavailable";

export interface SlackConnectLandingProps {
  sso: { google: boolean; microsoft: boolean };
  /** The signed-in person's email, or null when signed out. */
  email: string | null;
  /** Override for tests and stories. Defaults to the server action. */
  confirm?: typeof confirmSlackConnect;
}

/**
 * `/<locale>/integrations/slack/connect#<token>`: the link `/pairly connect` answers with
 * in Slack. Opening it changes nothing; the person signs in (if needed) and confirms that
 * this Slack account is theirs. The link works once and expires in 10 minutes; a used,
 * expired or someone else's link looks the same.
 */
export function SlackConnectLanding({ sso, email, confirm = confirmSlackConnect }: SlackConnectLandingProps) {
  const t = useTranslations("SlackConnect");
  const locale = useLocale();
  const [token, setToken] = useState<string | null>(null);
  const [view, setView] = useState<View>("loading");

  useEffect(() => {
    const fromHash = readSlackToken(window.location.hash);
    if (fromHash) {
      saveSlackToken(fromHash);
      window.history.replaceState(null, "", window.location.pathname);
    }
    const found = fromHash ?? loadSlackToken();
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (cancelled) return;
      setToken(found);
      setView(found === null ? "invalid" : email === null ? "signedOut" : "ready");
    });
    return () => {
      cancelled = true;
    };
  }, [email]);

  const connect = async () => {
    if (!token) return;
    setView("connecting");
    const result = await confirm(token);
    if (result.ok) {
      forgetSlackToken();
      setView("done");
    } else if (result.code === "gone") {
      forgetSlackToken();
      setView("invalid");
    } else if (result.code === "not_signed_in") {
      setView("signedOut");
    } else {
      setView("unavailable");
    }
  };

  switch (view) {
    case "loading":
      return (
        <AuthCard title={t("title")}>
          <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {t("loading")}
          </p>
        </AuthCard>
      );
    case "signedOut":
      return (
        <AuthCard title={t("signInTitle")} subtitle={t("signInBody")}>
          <SignInForm sso={sso} next={`/${locale}/integrations/slack/connect`} />
        </AuthCard>
      );
    case "invalid":
      return (
        <AuthCard title={t("invalidTitle")} subtitle={t("invalid")}>
          <Link2Off className="size-6 text-muted-foreground" aria-hidden />
          <p className="text-sm text-muted-foreground">{t("again")}</p>
        </AuthCard>
      );
    case "unavailable":
      return (
        <AuthCard title={t("unavailableTitle")} subtitle={t("unavailable")}>
          <Button variant="outline" onClick={() => setView("ready")}>
            {t("tryAgain")}
          </Button>
        </AuthCard>
      );
    case "done":
      return (
        <AuthCard title={t("doneTitle")}>
          <p role="status" className="flex items-start gap-2 text-sm" data-slack-connected>
            <CircleCheck className="mt-0.5 size-4 shrink-0 text-success-ink" aria-hidden />
            {t("done")}
          </p>
          <Button asChild variant="outline">
            <Link href="/inbox">{t("openInbox")}</Link>
          </Button>
        </AuthCard>
      );
    default:
      return (
        <AuthCard title={t("title")} subtitle={t("body")}>
          <p className="flex items-start gap-2 text-sm" data-slack-confirm>
            <MessageSquare className="mt-0.5 size-4 shrink-0" aria-hidden />
            {t("as", { email: isolate(email ?? "") })}
          </p>
          <p className="text-sm text-muted-foreground">{t("notYou")}</p>
          <Button onClick={() => void connect()} disabled={view === "connecting"} data-slack-connect>
            {view === "connecting" && <Loader2 className="animate-spin" aria-hidden />}
            {t("confirm")}
          </Button>
        </AuthCard>
      );
  }
}
