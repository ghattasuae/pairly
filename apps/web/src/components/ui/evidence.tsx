// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * A quoted piece of evidence: CV text behind a screening criterion, a line from a
 * document, the field an invoice check read. The quote sits on a ruled start edge, the
 * matched words are marked in saffron-soft with a bottom rule (so the mark never relies
 * on colour alone), and the source is a slate link under it. Screening and Pay both use
 * it, so evidence reads the same everywhere.
 */
export function Evidence({
  quote,
  highlight,
  source,
  sourceHref,
  meta,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<"figure">, "children"> & {
  /** The quoted text. A string can be marked with `highlight`; a node is shown as is. */
  quote: React.ReactNode;
  /** Words inside `quote` to mark (case-insensitive, every match). */
  highlight?: string | readonly string[];
  /** The label of the source link, for example "CV, page 2". */
  source?: React.ReactNode;
  sourceHref?: string;
  /** Quiet details after the source: a page, a confidence, a time. */
  meta?: React.ReactNode;
  /** Anything extra under the quote (a note, an action). */
  children?: React.ReactNode;
}) {
  return (
    <figure className={cn("evidence", className)} {...props}>
      <blockquote className="evidence-quote">
        {typeof quote === "string" ? <Marked text={quote} highlight={highlight} /> : quote}
      </blockquote>
      {(source || meta) && (
        <figcaption className="evidence-source">
          {source &&
            (sourceHref ? (
              <a href={sourceHref} className="link-inline">
                {source}
              </a>
            ) : (
              <span className="text-primary">{source}</span>
            ))}
          {meta && <span>{meta}</span>}
        </figcaption>
      )}
      {children}
    </figure>
  );
}

function escape(term: string): string {
  return term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Text with every occurrence of the highlight terms wrapped in an evidence mark. */
export function Marked({ text, highlight }: { text: string; highlight?: string | readonly string[] | undefined }) {
  const terms = (typeof highlight === "string" ? [highlight] : (highlight ?? [])).map((t) => t.trim()).filter(Boolean);
  if (terms.length === 0) return <>{text}</>;
  const pattern = new RegExp(`(${terms.map(escape).join("|")})`, "gi");
  const parts = text.split(pattern);
  return (
    <>
      {parts.map((part, i) =>
        // split() with one capture group puts the matches at the odd indexes.
        i % 2 === 1 ? (
          <mark key={i} className="evidence-mark">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}
