// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { confirmUnsubscribe, describeUnsubscribe } from "@/lib/actions/unsubscribe";
import { readUnsubscribeToken, type UnsubscribeView } from "@/lib/unsubscribe";

type State = { token: string; view: UnsubscribeView } | { done: boolean };

/**
 * `/<locale>/unsubscribe#token=...`. The token stays in the fragment (never sent to a
 * server by the browser). Opening the page changes nothing (mail scanners open links):
 * the person confirms. An expired link says so and offers sign-in to manage
 * notification settings, so opting out never fails silently.
 */
export function UnsubscribeLanding({
  describe = describeUnsubscribe,
  confirm = confirmUnsubscribe,
}: {
  describe?: (token: string) => Promise<UnsubscribeView>;
  confirm?: (token: string) => Promise<{ ok: boolean }>;
}) {
  const t = useTranslations("Unsubscribe");
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const token = readUnsubscribeToken(window.location.hash);
    if (token) window.history.replaceState(null, "", window.location.pathname);
    let cancelled = false;
    const found: Promise<UnsubscribeView> = token ? describe(token) : Promise.resolve({ kind: "invalid" });
    found
      .then((view) => !cancelled && setState({ token: token ?? "", view }))
      .catch(() => !cancelled && setState({ token: token ?? "", view: { kind: "unavailable" } }));
    return () => {
      cancelled = true;
    };
  }, [describe]);

  if (state === null) return null;

  const manage = (
    <Button asChild className="w-full">
      <Link href="/settings">{t("manage")}</Link>
    </Button>
  );

  if ("done" in state) {
    return (
      <AuthCard title={state.done ? t("doneTitle") : t("unavailableTitle")} subtitle={state.done ? t("done") : t("unavailable")}>
        {manage}
      </AuthCard>
    );
  }

  const { token, view } = state;
  if (view.kind === "valid") {
    const category = t.has(`categories.${view.category}` as "categories.digest")
      ? t(`categories.${view.category}` as "categories.digest")
      : view.category;
    return (
      <AuthCard title={t("validTitle")} subtitle={t("valid", { category })}>
        <div className="space-y-3">
          <Button
            className="w-full"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              const result = await confirm(token).catch(() => ({ ok: false }));
              setState({ done: result.ok });
              setBusy(false);
            }}
          >
            {busy ? t("confirming") : t("confirm")}
          </Button>
          <Button asChild variant="ghost" className="w-full">
            <Link href="/settings">{t("manageInstead")}</Link>
          </Button>
        </div>
      </AuthCard>
    );
  }
  const key = view.kind === "expired" ? "expired" : view.kind === "unavailable" ? "unavailable" : "invalid";
  return (
    <AuthCard title={t(`${key}Title`)} subtitle={t(key)}>
      {manage}
    </AuthCard>
  );
}
