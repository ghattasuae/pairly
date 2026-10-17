// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

import type { ReactNode } from "react";

export const metadata = { title: "Pairly quickstart (Next.js)" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
