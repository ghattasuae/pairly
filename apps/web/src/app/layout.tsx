// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import "./globals.css";

// The real root layout (with <html>) is app/[locale]/layout.tsx. This one only exists so
// app/not-found.tsx can render for paths outside any locale.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return children;
}
