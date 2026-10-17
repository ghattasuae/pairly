// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type en from "../../messages/en.json";

import type { Locale } from "./routing";

export type Messages = typeof en;

export async function loadMessages(locale: Locale): Promise<Messages> {
  switch (locale) {
    case "ar":
      return (await import("../../messages/ar.json")).default;
    default:
      return (await import("../../messages/en.json")).default;
  }
}
