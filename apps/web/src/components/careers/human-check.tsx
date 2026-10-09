// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

/**
 * The human check before an application is sent (brief 3.2.3).
 *
 * Bot protection is behind an interface on the API (`pairly_hire.antispam`). Until a
 * Cloudflare Turnstile site key and secret are set up, development and test use the
 * fake verifier, which accepts only Cloudflare's documented test token: this component
 * shows a plain checkbox that produces it. Production refuses the fake on the API side,
 * so nothing here can open applications without real bot protection.
 *
 * TODO(deploy): render the Turnstile widget when NEXT_PUBLIC_TURNSTILE_SITE_KEY is set
 * (needs challenges.cloudflare.com in the CSP script-src and frame-src).
 */
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Checkbox } from "@/components/ui/form-controls";
import { DEV_TURNSTILE_TOKEN } from "@/lib/careers/format";

export function HumanCheck({
  token,
  onToken,
  error,
}: {
  token: string | null;
  onToken: (token: string | null) => void;
  error?: string | null | undefined;
}) {
  const t = useTranslations("Careers");
  const id = useId();
  return (
    <fieldset className="space-y-2" aria-describedby={error ? `${id}-error` : undefined}>
      <legend className="text-sm font-medium">{t("humanCheck")}</legend>
      <label htmlFor={id} className="flex min-h-11 items-center gap-3 rounded-md border border-input bg-surface px-3">
        <Checkbox
          id={id}
          checked={token !== null}
          aria-invalid={error ? true : undefined}
          onChange={(event) => onToken(event.target.checked ? DEV_TURNSTILE_TOKEN : null)}
        />
        <span className="text-sm">{t("humanCheckDev")}</span>
      </label>
      {error && (
        <p id={`${id}-error`} className="text-sm text-destructive">
          {error}
        </p>
      )}
    </fieldset>
  );
}
