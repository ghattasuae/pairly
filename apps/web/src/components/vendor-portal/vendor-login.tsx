// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useRouter } from "@/i18n/navigation";
import { openVendorPortalSession } from "@/lib/actions/vendor-portal";
import { orgFromVendorToken } from "@/lib/vendor-portal/types";

import { VendorPortalSignIn } from "./vendor-sign-in";

/**
 * `/<locale>/vendor-portal/login#<magic link>`: the link from the contact's email. The
 * token stays in the fragment (browsers never send it to a server, so it never reaches a
 * log); this page posts it once, the server keeps the portal session in an HTTP-only
 * cookie, and the contact lands on their invoices. A used or expired link offers a new one.
 */
export function VendorPortalLogin({ open = openVendorPortalSession }: { open?: typeof openVendorPortalSession }) {
  const t = useTranslations("VendorPortal");
  const router = useRouter();
  const [failed, setFailed] = useState<{ org: string | null } | null>(null);
  // The link works once: never post it twice (development runs effects twice).
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = window.location.hash.replace(/^#/, "");
    window.history.replaceState(null, "", window.location.pathname);
    void open(token).then((result) => {
      if (result.ok) {
        router.replace("/vendor-portal");
        router.refresh();
      } else setFailed({ org: orgFromVendorToken(token) });
    });
  }, [open, router]);

  if (failed) return <VendorPortalSignIn org={failed.org} expired />;
  return (
    <p role="status" className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
      <Loader2 className="size-4 animate-spin" aria-hidden />
      {t("signIn.opening")}
    </p>
  );
}
