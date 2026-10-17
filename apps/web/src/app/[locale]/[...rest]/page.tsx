// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { notFound } from "next/navigation";

// Any unknown path inside a locale renders the localized not-found page.
export default function CatchAll() {
  notFound();
}
