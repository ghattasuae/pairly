// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

/** The frame for every sign-in step: centred, on ruled paper. */
export function AuthCard({ title, subtitle, children }: { title: React.ReactNode; subtitle?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="rise w-full max-w-sm space-y-6">
      <div className="space-y-2">
        <h1 className="font-display text-3xl leading-tight tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      {children}
    </div>
  );
}

export function FormError({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <p id={id} role="alert" className="rounded-md border border-destructive/30 bg-destructive/8 px-3 py-2 text-sm text-destructive">
      {children}
    </p>
  );
}
