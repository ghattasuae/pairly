// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Link, useRouter } from "@/i18n/navigation";
import { authClient } from "@/lib/auth/client";
import { safeNextPath } from "@/lib/safe-path";
import { cn } from "@/lib/utils";

/**
 * Shown when the API answers `second_factor_required`: approving, deciding, managing and
 * exporting need a sign-in with an authenticator code (brief 6.1).
 *
 * - TOTP on: sign in again (magic link, then the code) and come straight back to
 *   `returnPath`, for example the same inbox item.
 * - TOTP off: turn it on under Security first (that sign-in counts as a code sign-in).
 */
export function StepUpPrompt({
  totpEnabled,
  returnPath,
  className,
  compact = false,
  onSignOut,
}: {
  totpEnabled: boolean;
  /** A same-site path in this locale without query, e.g. `/en/inbox/<id>`. */
  returnPath: string;
  className?: string;
  compact?: boolean;
  /** Override for stories and tests. Defaults to Better Auth sign-out. */
  onSignOut?: () => Promise<void>;
}) {
  const t = useTranslations("StepUp");
  const router = useRouter();
  const [pending, setPending] = useState(false);

  const reauth = async () => {
    setPending(true);
    const next = safeNextPath(returnPath);
    if (onSignOut) await onSignOut();
    else await authClient.signOut();
    router.replace(next ? { pathname: "/sign-in", query: { next } } : "/sign-in");
    router.refresh();
  };

  return (
    <section
      role="region"
      aria-label={t("title")}
      className={cn(
        "relative rounded-xl border bg-surface ps-5 pe-4 py-4 before:absolute before:inset-y-3 before:start-0 before:w-1 before:rounded-e-full before:bg-signal",
        className,
      )}
    >
      <div className="flex items-start gap-3">
        <KeyRound className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden />
        <div className="min-w-0 space-y-2">
          <p className="font-medium">{t("title")}</p>
          {!compact && <p className="text-sm text-muted-foreground">{totpEnabled ? t("reauthBody") : t("setupBody")}</p>}
          <div className="pt-1">
            {totpEnabled ? (
              <Button size="sm" onClick={reauth} disabled={pending}>
                {t("reauthAction")}
              </Button>
            ) : (
              <Button asChild size="sm">
                <Link href="/settings/security">{t("setupAction")}</Link>
              </Button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
