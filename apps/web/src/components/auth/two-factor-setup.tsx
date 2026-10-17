// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ShieldCheck, ShieldOff } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useId, useMemo, useState } from "react";
import type * as React from "react";
import { renderSVG } from "uqr";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { authClient } from "@/lib/auth/client";

import { FormError } from "./auth-card";

type Step =
  | { kind: "idle" }
  | { kind: "scan"; totpURI: string; backupCodes: string[] }
  | { kind: "backup"; backupCodes: string[] };

/** The secret from an otpauth:// URI, for people who cannot scan. */
export function secretFromUri(uri: string): string {
  try {
    return new URL(uri).searchParams.get("secret") ?? "";
  } catch {
    return "";
  }
}

function groups(secret: string): string {
  return secret.replace(/(.{4})/g, "$1 ").trim();
}

/** Turn TOTP 2FA on (scan, confirm, save backup codes) or off. */
export function TwoFactorSetup({ enabled }: { enabled: boolean }) {
  const t = useTranslations("TwoFactor");
  const router = useRouter();
  const codeId = useId();
  const errorId = useId();
  const [step, setStep] = useState<Step>({ kind: "idle" });
  const [code, setCode] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<"invalid" | "freshSession" | null>(null);

  const qr = useMemo(
    () => (step.kind === "scan" ? renderSVG(step.totpURI, { border: 1, pixelSize: 5 }) : ""),
    [step],
  );

  const fail = (status?: number) => setError(status === 403 || status === 401 ? "freshSession" : "invalid");

  const start = async () => {
    setPending(true);
    setError(null);
    const { data, error: err } = await authClient.twoFactor.enable({} as Parameters<typeof authClient.twoFactor.enable>[0]);
    setPending(false);
    if (err || !data || !("totpURI" in data)) return fail(err?.status);
    setStep({ kind: "scan", totpURI: data.totpURI, backupCodes: data.backupCodes });
  };

  const confirm = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (step.kind !== "scan") return;
    setPending(true);
    setError(null);
    const { error: err } = await authClient.twoFactor.verifyTotp({ code });
    setPending(false);
    if (err) return fail(err.status);
    setStep({ kind: "backup", backupCodes: step.backupCodes });
  };

  const disable = async () => {
    setPending(true);
    setError(null);
    const { error: err } = await authClient.twoFactor.disable({} as Parameters<typeof authClient.twoFactor.disable>[0]);
    setPending(false);
    if (err) return fail(err.status);
    router.refresh();
  };

  return (
    <section aria-labelledby="two-factor-heading" className="max-w-2xl space-y-5 rounded-xl border bg-surface p-6 shadow-soft">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h2 id="two-factor-heading" className="font-display text-2xl">
            {t("setupTitle")}
          </h2>
          <p className="text-sm text-muted-foreground">{t("setupBody")}</p>
        </div>
        <Badge variant={enabled ? "default" : "outline"}>
          {enabled ? <ShieldCheck className="size-3.5" aria-hidden /> : <ShieldOff className="size-3.5" aria-hidden />}
          {enabled ? t("statusOn") : t("statusOff")}
        </Badge>
      </header>

      {error && <FormError id={errorId}>{t(error)}</FormError>}

      {step.kind === "idle" &&
        (enabled ? (
          <Button variant="outline" onClick={disable} disabled={pending}>
            {t("disable")}
          </Button>
        ) : (
          <Button onClick={start} disabled={pending}>
            {pending ? t("starting") : t("enable")}
          </Button>
        ))}

      {step.kind === "scan" && (
        <div className="grid gap-6 sm:grid-cols-[auto_1fr]">
          <div
            className="size-44 rounded-lg border bg-white p-2 text-black [&_svg]:size-full"
            role="img"
            aria-label={t("scan")}
            // uqr renders a self-contained SVG from the otpauth URI; no user HTML involved.
            dangerouslySetInnerHTML={{ __html: qr }}
          />
          <form onSubmit={confirm} className="space-y-4">
            <p className="text-sm">{t("scan")}</p>
            <div className="space-y-1 text-sm">
              <p className="text-muted-foreground">{t("manual")}</p>
              <code dir="ltr" className="block rounded-md bg-surface-sunken px-2 py-1.5 font-mono text-xs break-all">
                {groups(secretFromUri(step.totpURI))}
              </code>
            </div>
            <div className="space-y-2">
              <Label htmlFor={codeId}>{t("confirmLabel")}</Label>
              <Input
                id={codeId}
                dir="ltr"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={code}
                onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
                className="max-w-40 text-center font-mono tracking-[0.3em]"
              />
            </div>
            <Button type="submit" disabled={pending || code.length !== 6}>
              {t("confirm")}
            </Button>
          </form>
        </div>
      )}

      {step.kind === "backup" && (
        <div className="space-y-4">
          <p className="font-medium text-success">{t("enabled")}</p>
          <div className="space-y-1">
            <h3 className="font-semibold">{t("backupTitle")}</h3>
            <p className="text-sm text-muted-foreground">{t("backupBody")}</p>
          </div>
          <ul dir="ltr" className="grid grid-cols-2 gap-2 rounded-lg bg-surface-sunken p-4 font-mono text-sm sm:grid-cols-3">
            {step.backupCodes.map((backup) => (
              <li key={backup}>{backup}</li>
            ))}
          </ul>
          <Button
            onClick={() => {
              setStep({ kind: "idle" });
              router.refresh();
            }}
          >
            {t("done")}
          </Button>
        </div>
      )}
    </section>
  );
}
