// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { ClipboardCheck, FileSearch, House, Inbox, ReceiptText, Settings, type LucideIcon } from "lucide-react";

import type { NavKey } from "@/lib/nav";

export const NAV_ICONS: Record<NavKey, LucideIcon> = {
  home: House,
  inbox: Inbox,
  hire: ClipboardCheck,
  pay: ReceiptText,
  audit: FileSearch,
  settings: Settings,
};
