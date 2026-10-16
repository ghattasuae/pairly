// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Link, useRouter } from "@/i18n/navigation";
import { authClient } from "@/lib/auth/client";

/**
 * "Set up two-factor to continue". Shown to people whose role can approve, decide, manage
 * or export while their session has no TOTP sign-in: the API refuses those actions until
 * then (brief 6.1). `setup`: TOTP is off. `reauth`: TOTP is on but this session started
 * without a code, so they sign in again.
 */
export function SecondFactorNeeded({
  need,
  onSignOut,
}: {
  need: "setup" | "reauth";
  /** Override for stories and tests. Defaults to Better Auth sign-out. */
  onSignOut?: () => Promise<void>;
}) {
  const t = useTranslations("SecondFactor");
  const [pending, setPending] = useState(false);
  const router = useRouter();

  const signOutAndBack = async () => {
    setPending(true);
    if (onSignOut) await onSignOut();
    else {
      await authClient.signOut();
      router.replace("/sign-in");
      router.refresh();
    }
  };

  return (
    <section
      aria-labelledby="second-factor-heading"
      className="relative rounded-xl border bg-surface ps-6 pe-5 py-5 shadow-soft before:absolute before:inset-y-4 before:start-0 before:w-1 before:rounded-e-full before:bg-signal"
    >
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border bg-background text-primary">
          <KeyRound className="size-5" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          <p className="eyebrow">{t("eyebrow")}</p>
          <h2 id="second-factor-heading" className="font-display text-2xl leading-tight">
            {need === "setup" ? t("setupTitle") : t("reauthTitle")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {need === "setup" ? t("setupBody") : t("reauthBody")} {t("canStill")}
          </p>
          <div className="pt-2">
            {need === "setup" ? (
              <Button asChild>
                <Link href="/settings/security">{t("setupAction")}</Link>
              </Button>
            ) : (
              <Button onClick={signOutAndBack} disabled={pending}>
                {t("reauthAction")}
              </Button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
