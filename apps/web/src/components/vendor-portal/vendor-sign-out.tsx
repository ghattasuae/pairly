// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { LogOut } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useRouter } from "@/i18n/navigation";
import { signOutOfVendorPortal } from "@/lib/actions/vendor-portal";

export function VendorPortalSignOut() {
  const t = useTranslations("VendorPortal");
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        await signOutOfVendorPortal();
        router.replace("/vendor-portal?signed_out=1");
        router.refresh();
      }}
    >
      <LogOut className="rtl:-scale-x-100" aria-hidden />
      {t("nav.signOut")}
    </Button>
  );
}
