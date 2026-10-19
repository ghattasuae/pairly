// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

/** The frame for every sign-in step: centred on paper, title in Fraunces. */
export function AuthCard({ title, subtitle, children }: { title: React.ReactNode; subtitle?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="rise w-full max-w-sm space-y-7">
      <div className="space-y-2.5">
        <h1 className="font-display text-[2.25rem] leading-[1.1] font-normal text-balance">{title}</h1>
        {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      {children}
    </div>
  );
}

export function FormError({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <p id={id} role="alert" className="rounded-md border border-destructive/30 bg-danger-soft px-3 py-2 text-sm text-danger-ink">
      {children}
    </p>
  );
}
