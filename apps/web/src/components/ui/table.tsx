// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * The shared data table. A hairline frame that scrolls sideways on small screens, a
 * sticky header on sunken paper, hairline rows with a hover wash and mono tabular
 * numbers (`numeric` cells, or `data-num` on any cell). `dense` tightens rows for long
 * screening and audit lists. Give the frame a `maxHeight` (or a max-h class) when the
 * header should stay visible while the rows scroll.
 *
 * The parts are plain elements with classes from `globals.css` (`.table-frame`,
 * `.data-table`), so an existing `<table>` can adopt the look by class alone.
 */
export function Table({
  className,
  frameClassName,
  dense = false,
  maxHeight,
  label,
  ...props
}: React.ComponentProps<"table"> & {
  frameClassName?: string;
  dense?: boolean;
  /** A CSS length; the header sticks while the body scrolls inside it. */
  maxHeight?: string;
  /** Names the scroll region for keyboard and screen reader users when it scrolls. */
  label?: string;
}) {
  return (
    <div
      className={cn("table-frame", maxHeight && "overflow-y-auto", frameClassName)}
      style={maxHeight ? { maxHeight } : undefined}
      // A scrolling region must be reachable by keyboard (axe scrollable-region-focusable).
      {...(label ? { role: "region", "aria-label": label, tabIndex: 0 } : {})}
    >
      <table className={cn("data-table", dense && "data-table-dense", className)} {...props} />
    </div>
  );
}

export function TableHead(props: React.ComponentProps<"thead">) {
  return <thead {...props} />;
}

export function TableBody(props: React.ComponentProps<"tbody">) {
  return <tbody {...props} />;
}

export function TableRow(props: React.ComponentProps<"tr">) {
  return <tr {...props} />;
}

export function TableHeaderCell({
  className,
  numeric = false,
  scope = "col",
  ...props
}: React.ComponentProps<"th"> & { numeric?: boolean }) {
  return <th scope={scope} className={cn(numeric && "text-end", className)} {...props} />;
}

export function TableCell({ className, numeric = false, ...props }: React.ComponentProps<"td"> & { numeric?: boolean }) {
  return <td className={cn(numeric && "cell-num", className)} {...props} />;
}
