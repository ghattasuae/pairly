// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { LogOut } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useRouter } from "@/i18n/navigation";
import { signOutOfPortal } from "@/lib/actions/portal";

export function PortalSignOut() {
  const t = useTranslations("Portal");
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
        await signOutOfPortal();
        router.replace("/portal?signed_out=1");
        router.refresh();
      }}
    >
      <LogOut className="rtl:-scale-x-100" aria-hidden />
      {t("nav.signOut")}
    </Button>
  );
}
