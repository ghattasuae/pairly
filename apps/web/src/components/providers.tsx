// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ThemeProvider } from "next-themes";
import type * as React from "react";

import { TooltipProvider } from "@/components/ui/tooltip";

export function Providers({ children, nonce }: { children: React.ReactNode; nonce?: string | undefined }) {
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      {...(nonce ? { nonce } : {})}
    >
      <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
    </ThemeProvider>
  );
}
