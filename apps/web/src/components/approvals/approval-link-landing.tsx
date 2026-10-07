// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { CircleCheck, Link2Off, Loader2 } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useState } from "react";

import { AuthCard, FormError } from "@/components/auth/auth-card";
import { SignInForm } from "@/components/auth/sign-in-form";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { Link } from "@/i18n/navigation";
import {
  confirmApprovalLink,
  describeApprovalLink,
  type LinkDescription,
} from "@/lib/actions/approvals";
import type { ActionResult } from "@/lib/action-result";
import type { ApprovalLinkView, DecisionResult } from "@/lib/api/types";
import { forgetLinkToken, loadLinkToken, readLinkToken, saveLinkToken } from "@/lib/approval-link-token";
import { authClient } from "@/lib/auth/client";
import { isolate } from "@/lib/bidi";

type View =
  | { kind: "loading" }
  | LinkDescription
  | { kind: "needsFactor"; link: ApprovalLinkView; email: string | null }
  | { kind: "done"; link: ApprovalLinkView; result: DecisionResult };

export interface ApprovalLinkLandingProps {
  sso: { google: boolean; microsoft: boolean };
  totpEnabled: boolean;
  /** Overrides for tests and stories. Default to the server actions. */
  describe?: (token: string) => Promise<LinkDescription>;
  confirm?: (token: string, reason: string | null) => Promise<ActionResult<{ result: DecisionResult }>>;
}

/**
 * `/<locale>/approvals/link#<token>`: an approval link from an email or Slack.
 *
 * Opening it changes nothing (mail scanners open links). The page says exactly what the
 * link would do, for the person it was sent to, after they sign in (with their
 * authenticator code: approving needs it). Only the explicit button decides. Expired,
 * used, invalid and someone else's links all look the same, so a link never reveals
 * anything to the wrong person.
 */
export function ApprovalLinkLanding({
  sso,
  totpEnabled,
  describe = describeApprovalLink,
  confirm = confirmApprovalLink,
}: ApprovalLinkLandingProps) {
  const t = useTranslations("ApprovalLink");
  const locale = useLocale();
  const [token, setToken] = useState<string | null>(null);
  const [view, setView] = useState<View>({ kind: "loading" });

  useEffect(() => {
    const fromHash = readLinkToken(window.location.hash);
    if (fromHash) {
      saveLinkToken(fromHash);
      window.history.replaceState(null, "", window.location.pathname);
    }
    const found = fromHash ?? loadLinkToken();
    let cancelled = false;
    const described: Promise<LinkDescription> = found
      ? describe(found)
      : Promise.resolve({ kind: "invalid", email: null });
    described
      .then((result) => {
        if (cancelled) return;
        setToken(found);
        // An "invalid" link is kept (for its 30 minutes at most): it may be someone
        // else's sign-in, and the right person can sign in from this browser.
        if (result.kind === "ready" && !result.link.has_second_factor) {
          setView({ kind: "needsFactor", link: result.link, email: result.email });
        } else {
          setView(result);
        }
      })
      .catch(() => !cancelled && setView({ kind: "unavailable" }));
    return () => {
      cancelled = true;
    };
  }, [describe]);

  const returnPath = `/${locale}/approvals/link`;

  switch (view.kind) {
    case "loading":
      return (
        <AuthCard title={t("loadingTitle")}>
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {t("loading")}
          </p>
        </AuthCard>
      );
    case "signedOut":
      return (
        <AuthCard title={t("signInTitle")} subtitle={t("signInBody")}>
          <SignInForm sso={sso} next={returnPath} />
        </AuthCard>
      );
    case "invalid":
      return <InvalidLink email={view.email} />;
    case "unavailable":
      return (
        <AuthCard title={t("unavailableTitle")} subtitle={t("unavailable")}>
          <Button variant="outline" onClick={() => window.location.reload()}>
            {t("tryAgain")}
          </Button>
        </AuthCard>
      );
    case "needsFactor":
      return (
        <AuthCard title={t(`action.${actionKey(view.link.action)}.title`)}>
          <Summary link={view.link} />
          <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} />
        </AuthCard>
      );
    case "done":
      return (
        <AuthCard title={t("doneTitle")}>
          <p role="status" className="flex items-start gap-2 text-sm">
            <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" aria-hidden />
            {t(`done.${actionKey(view.link.action)}`, { title: view.link.title })}
          </p>
          <p className="text-sm text-muted-foreground">{t("doneBody")}</p>
          <Button asChild variant="outline">
            <Link href={`/inbox/${view.link.approval_id}`}>{t("openInbox")}</Link>
          </Button>
        </AuthCard>
      );
    case "ready":
      return (
        <ReadyLink
          token={token ?? ""}
          link={view.link}
          email={view.email}
          confirm={confirm}
          onDone={(result) => {
            forgetLinkToken();
            setView({ kind: "done", link: view.link, result });
          }}
          onInvalid={() => {
            forgetLinkToken();
            setView({ kind: "invalid", email: view.email });
          }}
          onNeedsFactor={() => setView({ kind: "needsFactor", link: view.link, email: view.email })}
        />
      );
  }
}

function actionKey(action: string): "approve" | "reject" | "request_changes" {
  return action === "reject" || action === "request_changes" ? action : "approve";
}

function Summary({ link }: { link: ApprovalLinkView }) {
  const t = useTranslations("ApprovalLink");
  const workflows = useTranslations("Workflows");
  const format = useFormatter();
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 rounded-xl border bg-surface p-4 text-sm">
      <dt className="text-muted-foreground">{t("item")}</dt>
      <dd className="font-medium">
        <bdi>{link.title}</bdi>
      </dd>
      <dt className="text-muted-foreground">{t("workflow")}</dt>
      <dd>{workflows.has(link.workflow as "pay") ? workflows(link.workflow as "pay") : link.workflow}</dd>
      <dt className="text-muted-foreground">{t("step")}</dt>
      <dd>
        <bdi>{link.step}</bdi>
      </dd>
      <dt className="text-muted-foreground">{t("expires")}</dt>
      <dd>{format.relativeTime(new Date(link.expires_at), new Date())}</dd>
    </dl>
  );
}

function ReadyLink({
  token,
  link,
  email,
  confirm,
  onDone,
  onInvalid,
  onNeedsFactor,
}: {
  token: string;
  link: ApprovalLinkView;
  email: string | null;
  confirm: NonNullable<ApprovalLinkLandingProps["confirm"]>;
  onDone: (result: DecisionResult) => void;
  onInvalid: () => void;
  onNeedsFactor: () => void;
}) {
  const t = useTranslations("ApprovalLink");
  const tErrors = useTranslations("Errors");
  const action = actionKey(link.action);
  const reasonId = useId();
  const errorId = useId();
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reasonNeeded = action !== "approve";

  const submit = async () => {
    if (reasonNeeded && reason.trim().length === 0) return;
    setPending(true);
    setError(null);
    try {
      const result = await confirm(token, reason.trim() || null);
      if (result.ok) return onDone(result.result);
      if (result.code === "gone") return onInvalid();
      if (result.code === "second_factor_required") return onNeedsFactor();
      setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  return (
    <AuthCard title={t(`action.${action}.title`)} subtitle={t(`action.${action}.body`)}>
      <Summary link={link} />
      {email && <p className="text-xs text-muted-foreground">{t("signedInAs", { email: isolate(email) })}</p>}
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {reasonNeeded && (
          <div className="space-y-2">
            <label htmlFor={reasonId} className="text-sm font-medium">
              {t("reasonLabel")}
            </label>
            <Textarea
              id={reasonId}
              required
              maxLength={2000}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>
        )}
        {error && <FormError id={errorId}>{tErrors(errorKeyFor(error) as "generic")}</FormError>}
        <Button
          type="submit"
          size="lg"
          className="w-full"
          variant={action === "reject" ? "destructive" : "default"}
          disabled={pending || (reasonNeeded && reason.trim().length === 0)}
          aria-describedby={error ? errorId : undefined}
        >
          {pending ? t("confirming") : t(`action.${action}.confirm`)}
        </Button>
        <p className="text-xs text-muted-foreground">{t("finalNote")}</p>
      </form>
    </AuthCard>
  );
}

function errorKeyFor(code: string): string {
  return ["segregation_of_duties", "not_assigned", "already_decided", "rate_limited", "unavailable"].includes(code)
    ? code
    : "generic";
}

function InvalidLink({ email }: { email: string | null }) {
  const t = useTranslations("ApprovalLink");
  const [pending, setPending] = useState(false);
  const signOut = async () => {
    setPending(true);
    await authClient.signOut();
    window.location.reload();
  };
  return (
    <AuthCard title={t("invalidTitle")} subtitle={t("invalid")}>
      <Link2Off className="size-6 text-muted-foreground" aria-hidden />
      {email && <p className="text-sm text-muted-foreground">{t("invalidSignedIn", { email: isolate(email) })}</p>}
      <div className="flex flex-wrap gap-2">
        <Button asChild>
          <Link href="/inbox">{t("openInbox")}</Link>
        </Button>
        {email && (
          <Button variant="outline" onClick={signOut} disabled={pending}>
            {t("signOut")}
          </Button>
        )}
      </div>
    </AuthCard>
  );
}
