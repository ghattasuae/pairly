// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

export function PageHeader({ eyebrow, title, children }: { eyebrow?: React.ReactNode; title: React.ReactNode; children?: React.ReactNode }) {
  return (
    <header className="rise space-y-2" style={{ "--rise-index": 0 } as React.CSSProperties}>
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h1 className="font-display text-3xl leading-tight tracking-tight sm:text-4xl">{title}</h1>
      {children && <div className="text-muted-foreground">{children}</div>}
    </header>
  );
}
