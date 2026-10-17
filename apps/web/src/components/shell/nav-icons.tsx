// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { ClipboardCheck, FileSearch, House, Inbox, Receipt, ReceiptText, Settings, type LucideIcon } from "lucide-react";

import type { NavKey } from "@/lib/nav";

export const NAV_ICONS: Record<NavKey, LucideIcon> = {
  home: House,
  inbox: Inbox,
  expense: Receipt,
  hire: ClipboardCheck,
  pay: ReceiptText,
  audit: FileSearch,
  settings: Settings,
};
