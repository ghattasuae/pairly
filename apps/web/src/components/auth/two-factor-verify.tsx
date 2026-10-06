// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Link } from "@/i18n/navigation";
import { authClient } from "@/lib/auth/client";

import { AuthCard, FormError } from "./auth-card";

type Mode = "totp" | "backup";
type Failure = "invalid" | "expired";

const RESTART_CODES = new Set(["INVALID_TWO_FACTOR_COOKIE", "TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE"]);

/**
 * Second step after a magic link or SSO sign-in for users with 2FA on. The pending sign-in
 * lives in the signed two_factor cookie; a valid code turns it into a session.
 */
export function TwoFactorVerify({
  verify,
}: {
  /** Override for stories and tests. Defaults to Better Auth. */
  verify?: (mode: Mode, code: string) => Promise<{ ok: boolean; code?: string | undefined }>;
}) {
  const t = useTranslations("TwoFactor");
  const codeId = useId();
  const errorId = useId();
  const [mode, setMode] = useState<Mode>("totp");
  const [code, setCode] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);

  const run =
    verify ??
    (async (m: Mode, value: string) => {
      const { error } =
        m === "totp"
          ? await authClient.twoFactor.verifyTotp({ code: value })
          : await authClient.twoFactor.verifyBackupCode({ code: value });
      return error ? { ok: false, code: error.code } : { ok: true };
    });

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPending(true);
    setFailure(null);
    try {
      const result = await run(mode, code.trim());
      if (result.ok) {
        // Full navigation so the server renders with the new session cookie.
        window.location.assign(window.location.pathname.replace(/\/two-factor$/, "") || "/");
        return;
      }
      // The pending sign-in (two_factor cookie) is gone or used up: start again.
      setFailure(result.code && RESTART_CODES.has(result.code) ? "expired" : "invalid");
    } catch {
      setFailure("invalid");
    }
    setPending(false);
  };

  return (
    <AuthCard title={t("verifyTitle")} subtitle={t("verifyBody")}>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <div className="space-y-2">
          <Label htmlFor={codeId}>{mode === "totp" ? t("codeLabel") : t("backupLabel")}</Label>
          <Input
            id={codeId}
            dir="ltr"
            autoComplete="one-time-code"
            inputMode={mode === "totp" ? "numeric" : "text"}
            pattern={mode === "totp" ? "[0-9]{6}" : undefined}
            maxLength={mode === "totp" ? 6 : 32}
            autoFocus
            value={code}
            onChange={(event) => setCode(event.target.value.replace(mode === "totp" ? /\D/g : /\s/g, ""))}
            aria-invalid={failure ? true : undefined}
            aria-describedby={failure ? errorId : undefined}
            className="text-center font-mono text-lg tracking-[0.4em]"
          />
        </div>
        {failure && (
          <FormError id={errorId}>
            {t(failure)}{" "}
            {failure === "expired" && (
              <Link href="/sign-in" className="font-medium underline">
                {t("startOver")}
              </Link>
            )}
          </FormError>
        )}
        <Button type="submit" size="lg" className="w-full" disabled={pending || code.length < 6}>
          {pending ? t("verifying") : t("verify")}
        </Button>
      </form>
      <div className="flex justify-between text-sm">
        <button
          type="button"
          className="font-medium text-primary underline-offset-4 hover:underline"
          onClick={() => {
            setMode(mode === "totp" ? "backup" : "totp");
            setCode("");
            setFailure(null);
          }}
        >
          {mode === "totp" ? t("useBackup") : t("useApp")}
        </button>
        <Link href="/sign-in" className="text-muted-foreground underline-offset-4 hover:underline">
          {t("startOver")}
        </Link>
      </div>
    </AuthCard>
  );
}
