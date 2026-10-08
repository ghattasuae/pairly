// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { FileWarning, Minus, Plus, ScanSearch, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import type { PDFDocumentProxy, PDFPageProxy, RenderTask } from "pdfjs-dist";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { humanizeKey } from "@/components/data/json-view";
import { useExplanation } from "@/components/data/explain";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { documentSource } from "@/lib/actions/documents";
import type { ActionResult } from "@/lib/action-result";
import type { BBox } from "@/lib/api/types";
import { findingsFirst, type Highlight, type Subject } from "@/lib/evidence";
import { cn } from "@/lib/utils";

import { GuardrailNotice } from "./guardrail-notice";
import { type OpenedPdf, openPdf } from "./pdf";

type Source = { url: string; mediaType: string; filename?: string | null };
type LoadSource = (id: string) => Promise<ActionResult<{ url: string; mediaType: string; filename: string | null }>>;

type Phase =
  | { kind: "loading" }
  | { kind: "error"; code: string }
  | { kind: "unsupported"; filename: string | null }
  | { kind: "pdf"; doc: PDFDocumentProxy; sizes: { width: number; height: number }[] }
  | { kind: "image"; url: string };

const ZOOMS = [0.75, 1, 1.25, 1.5, 2] as const;
const MAX_PAGES = 300;

export interface DocumentViewerProps {
  documentId: string;
  /** Evidence quotes and guardrail findings to draw on the pages. */
  highlights?: readonly Highlight[];
  /** Page to show first (1-based). */
  initialPage?: number;
  /** `split`: pages beside the list (full page). `stacked`: list under the pages (panels). */
  layout?: "split" | "stacked";
  className?: string;
  /** What the document is, for the explanations ("This invoice ..."). */
  subject?: Subject;
  /**
   * Show a highlight on the page (scroll to it and mark it) whenever `seq` changes, for
   * controls outside the viewer such as the inbox warning.
   */
  focus?: { id: string; seq: number } | null;
  /** Show the guardrail warning above the pages (the document page). */
  notice?: boolean;
  /** Overrides for stories and tests. */
  loadSource?: LoadSource;
  source?: Source;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

/**
 * The document viewer (brief 5.2.3): the document's pages, with evidence quotes and
 * guardrail findings drawn where they were found. Selecting an item in the list scrolls
 * to it and marks it. PDFs render with PDF.js; images as they are. The bytes come through
 * a short-lived signed URL requested just before rendering.
 *
 * Accessibility: pages are images to assistive technology ("Page 2 of 5"), so every
 * highlight is also listed as text with its page, and every control is a real button.
 * Box coordinates are physical (from the page's left edge), so the page area is LTR in
 * every locale; the list and toolbar follow the page direction.
 */
export function DocumentViewer({
  documentId,
  highlights = [],
  initialPage = 1,
  layout = "stacked",
  className,
  subject = "document",
  focus = null,
  notice = false,
  loadSource = documentSource,
  source,
}: DocumentViewerProps) {
  const t = useTranslations("Viewer");
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [zoom, setZoom] = useState<number>(1);
  const [width, setWidth] = useState(0);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(initialPage);
  const scroller = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef(new Map<number, HTMLElement>());

  // Load: signed URL, then the bytes (PDF) or an <img> (images).
  useEffect(() => {
    let cancelled = false;
    let opened: OpenedPdf | null = null;
    (async () => {
      let src: Source;
      if (source) src = source;
      else {
        const result = await loadSource(documentId);
        if (!result.ok) return !cancelled && setPhase({ kind: "error", code: result.code });
        src = result;
      }
      if (src.mediaType.startsWith("image/")) return !cancelled && setPhase({ kind: "image", url: src.url });
      if (src.mediaType !== "application/pdf") {
        return !cancelled && setPhase({ kind: "unsupported", filename: src.filename ?? null });
      }
      const response = await fetch(src.url, { credentials: "omit", referrerPolicy: "no-referrer" });
      if (!response.ok) throw new Error(`document ${response.status}`);
      opened = await openPdf(await response.arrayBuffer());
      const doc = opened.doc;
      const count = Math.min(doc.numPages, MAX_PAGES);
      const sizes = await Promise.all(
        Array.from({ length: count }, async (_, i) => {
          const page = await doc.getPage(i + 1);
          const viewport = page.getViewport({ scale: 1 });
          return { width: viewport.width, height: viewport.height };
        }),
      );
      if (!cancelled) setPhase({ kind: "pdf", doc, sizes });
    })().catch(() => {
      if (!cancelled) setPhase({ kind: "error", code: "unavailable" });
    });
    return () => {
      cancelled = true;
      void opened?.destroy();
    };
    // The source is compared by value: a new object with the same URL is the same document.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, loadSource, source?.url, source?.mediaType]);

  // Fit the page width to the viewer.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(0, (entry?.contentRect.width ?? 0) - 2)));
    observer.observe(el);
    return () => observer.disconnect();
  }, [phase.kind]);

  const pageCount = phase.kind === "pdf" ? phase.sizes.length : phase.kind === "image" ? 1 : 0;

  const scrollToPage = useCallback((page: number) => {
    const el = pageRefs.current.get(page);
    el?.scrollIntoView({ block: "start", behavior: prefersReducedMotion() ? "auto" : "smooth" });
  }, []);

  // The first page to show, once the pages exist.
  const jumped = useRef(false);
  useEffect(() => {
    if (jumped.current || pageCount === 0 || initialPage <= 1) return;
    jumped.current = true;
    requestAnimationFrame(() => scrollToPage(Math.min(initialPage, pageCount)));
  }, [pageCount, initialPage, scrollToPage]);

  const scrollToHighlight = useCallback(
    (highlight: Highlight) => {
      if (highlight.page === null) return;
      requestAnimationFrame(() => {
        const box = scroller.current?.querySelector<HTMLElement>(`[data-highlight="${CSS.escape(highlight.id)}"]`);
        const behavior = prefersReducedMotion() ? "auto" : "smooth";
        if (box) box.scrollIntoView({ block: "center", inline: "nearest", behavior });
        else scrollToPage(highlight.page ?? 1);
      });
    },
    [scrollToPage],
  );

  const focusHighlight = useCallback(
    (highlight: Highlight) => {
      setActiveId(highlight.id);
      scrollToHighlight(highlight);
    },
    [scrollToHighlight],
  );

  // A request from outside the viewer (the inbox warning) to show one highlight: mark it
  // while rendering, scroll to it after. Only a new request (`seq`) moves the view.
  const [seenFocus, setSeenFocus] = useState<number | null>(null);
  if (focus && focus.seq !== seenFocus) {
    setSeenFocus(focus.seq);
    if (highlights.some((h) => h.id === focus.id)) setActiveId(focus.id);
  }
  const focusTarget = focus ? highlights.find((h) => h.id === focus.id) : undefined;
  useEffect(() => {
    if (focusTarget) scrollToHighlight(focusTarget);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.seq]);

  const listed = useMemo(() => findingsFirst(highlights), [highlights]);

  const byPage = useMemo(() => {
    const map = new Map<number, Highlight[]>();
    for (const h of highlights) {
      if (h.page === null || h.boxes.length === 0) continue;
      map.set(h.page, [...(map.get(h.page) ?? []), h]);
    }
    return map;
  }, [highlights]);

  const registerPage = useCallback((page: number, el: HTMLElement | null) => {
    if (el) pageRefs.current.set(page, el);
    else pageRefs.current.delete(page);
  }, []);

  // Which page is in view, for the "Page 2 of 5" indicator.
  useEffect(() => {
    const root = scroller.current;
    if (!root || pageCount === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        const page = Number((visible?.target as HTMLElement | undefined)?.dataset.page);
        if (page) setCurrentPage(page);
      },
      { root, threshold: [0.25, 0.5, 0.75] },
    );
    for (const el of pageRefs.current.values()) observer.observe(el);
    return () => observer.disconnect();
  }, [pageCount, width]);

  const zoomIndex = ZOOMS.indexOf(zoom as (typeof ZOOMS)[number]);
  const hasEvidence = highlights.some((h) => h.kind === "evidence");
  const hasFindings = highlights.some((h) => h.kind === "guardrail");

  const pages = (
    <div className="flex min-w-0 flex-col overflow-hidden rounded-xl border bg-surface-sunken">
      <div
        role="toolbar"
        aria-label={t("toolbar")}
        className="flex flex-wrap items-center gap-2 border-b bg-surface px-3 py-2 text-sm"
      >
        <span aria-live="polite" className="me-auto text-muted-foreground">
          {pageCount > 0 ? t("pageOf", { page: Math.min(currentPage, pageCount), count: pageCount }) : t("title")}
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label={t("zoomOut")}
          disabled={pageCount === 0 || zoomIndex <= 0}
          onClick={() => setZoom(ZOOMS[Math.max(0, zoomIndex - 1)] ?? 1)}
        >
          <Minus aria-hidden />
        </Button>
        <span className="w-12 text-center font-mono text-xs tabular-nums" dir="ltr" aria-label={t("zoomLevel")}>
          {Math.round(zoom * 100)}%
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label={t("zoomIn")}
          disabled={pageCount === 0 || zoomIndex >= ZOOMS.length - 1}
          onClick={() => setZoom(ZOOMS[Math.min(ZOOMS.length - 1, zoomIndex + 1)] ?? 1)}
        >
          <Plus aria-hidden />
        </Button>
      </div>
      <div
        ref={scroller}
        dir="ltr"
        tabIndex={0}
        aria-label={t("pages")}
        className={cn("relative overflow-auto p-3 outline-none", layout === "split" ? "h-[75vh]" : "max-h-[60vh] min-h-64")}
      >
        {phase.kind === "loading" && <p className="p-6 text-center text-sm text-muted-foreground">{t("loading")}</p>}
        {phase.kind === "error" && (
          <p role="alert" className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
            <FileWarning className="size-4" aria-hidden />
            {phase.code === "conflict" ? t("notScanned") : phase.code === "not_found" ? t("notFound") : t("error")}
          </p>
        )}
        {phase.kind === "unsupported" && (
          <p className="p-6 text-center text-sm text-muted-foreground">
            {t("unsupported", { name: phase.filename ?? t("thisFile") })}
          </p>
        )}
        {phase.kind === "pdf" && width > 0 && (
          <div className="mx-auto flex flex-col items-center gap-3">
            {phase.sizes.map((size, index) => (
              <PdfPage
                key={index + 1}
                doc={phase.doc}
                number={index + 1}
                count={phase.sizes.length}
                size={size}
                scale={((width - 4) * zoom) / size.width}
                highlights={byPage.get(index + 1) ?? []}
                activeId={activeId}
                register={registerPage}
              />
            ))}
          </div>
        )}
        {phase.kind === "image" && width > 0 && (
          <ImagePage
            url={phase.url}
            width={(width - 4) * zoom}
            highlights={byPage.get(1) ?? []}
            activeId={activeId}
            register={registerPage}
          />
        )}
      </div>
    </div>
  );

  const list = (
    <div className="min-w-0 space-y-4">
      {(hasEvidence || hasFindings) && (
        <ul aria-label={t("legend")} className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {hasEvidence && (
            <li className="flex items-center gap-1.5">
              <span aria-hidden className="inline-block h-3 w-5 rounded-[2px] border border-primary/70 bg-primary/20" />
              {t("legendEvidence")}
            </li>
          )}
          {hasFindings && (
            <li className="flex items-center gap-1.5">
              <span aria-hidden className="highlight-guardrail inline-block h-3 w-5 rounded-[2px]" />
              {t("legendGuardrail")}
            </li>
          )}
        </ul>
      )}
      <HighlightList highlights={listed} activeId={activeId} onSelect={focusHighlight} subject={subject} />
    </div>
  );

  const withNotice = notice && hasFindings;
  const body = (
    <div
      className={cn(
        layout === "split" ? "grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]" : "space-y-4",
        withNotice ? undefined : className,
      )}
    >
      {pages}
      {highlights.length > 0 && list}
    </div>
  );
  if (!withNotice) return body;
  return (
    <div className={cn("space-y-4", className)}>
      <GuardrailNotice highlights={highlights} subject={subject} onShow={focusHighlight} headingLevel={2} />
      {body}
    </div>
  );
}

function Box({ box, scale, highlight, active }: { box: BBox; scale: number; highlight: Highlight; active: boolean }) {
  return (
    <span
      aria-hidden
      data-highlight={highlight.id}
      className={cn(
        "pointer-events-none absolute rounded-[2px]",
        highlight.kind === "evidence"
          ? "border border-primary/70 bg-primary/20"
          : "highlight-guardrail",
        active && "highlight-active",
      )}
      // Physical coordinates: boxes are measured from the page's top-left corner.
      style={{
        left: box.x0 * scale - 1,
        top: box.top * scale - 1,
        width: (box.x1 - box.x0) * scale + 2,
        height: (box.bottom - box.top) * scale + 2,
      }}
    />
  );
}

function Overlays({ highlights, scale, activeId }: { highlights: readonly Highlight[]; scale: number; activeId: string | null }) {
  return (
    <>
      {highlights.flatMap((h) =>
        h.boxes.map((box, i) => <Box key={`${h.id}:${i}`} box={box} scale={scale} highlight={h} active={h.id === activeId} />),
      )}
    </>
  );
}

function PdfPage({
  doc,
  number,
  count,
  size,
  scale,
  highlights,
  activeId,
  register,
}: {
  doc: PDFDocumentProxy;
  number: number;
  count: number;
  size: { width: number; height: number };
  scale: number;
  highlights: readonly Highlight[];
  activeId: string | null;
  register: (page: number, el: HTMLElement | null) => void;
}) {
  const t = useTranslations("Viewer");
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const holder = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(number <= 2);
  const cssWidth = Math.floor(size.width * scale);
  const cssHeight = Math.floor(size.height * scale);

  useEffect(() => {
    const el = holder.current;
    if (!el) return;
    register(number, el);
    const observer = new IntersectionObserver(([entry]) => entry?.isIntersecting && setVisible(true), {
      rootMargin: "400px 0px",
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      register(number, null);
    };
  }, [number, register]);

  useEffect(() => {
    if (!visible || !canvas.current) return;
    let task: RenderTask | null = null;
    let cancelled = false;
    doc
      .getPage(number)
      .then((page: PDFPageProxy) => {
        if (cancelled || !canvas.current) return;
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const viewport = page.getViewport({ scale: scale * ratio });
        const el = canvas.current;
        el.width = Math.floor(viewport.width);
        el.height = Math.floor(viewport.height);
        task = page.render({ canvas: el, viewport });
        return task.promise;
      })
      .catch(() => {
        /* a cancelled render (zoom changed) is expected */
      });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, number, scale, visible]);

  return (
    <div
      ref={holder}
      data-page={number}
      role="img"
      aria-label={t("pageOf", { page: number, count })}
      className="relative shrink-0 bg-white shadow-soft"
      style={{ width: cssWidth, height: cssHeight }}
    >
      <canvas ref={canvas} className="block size-full" />
      <Overlays highlights={highlights} scale={scale} activeId={activeId} />
    </div>
  );
}

function ImagePage({
  url,
  width,
  highlights,
  activeId,
  register,
}: {
  url: string;
  width: number;
  highlights: readonly Highlight[];
  activeId: string | null;
  register: (page: number, el: HTMLElement | null) => void;
}) {
  const t = useTranslations("Viewer");
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  const holder = useCallback((el: HTMLDivElement | null) => register(1, el), [register]);
  const scale = natural ? width / natural.width : 1;
  return (
    <div ref={holder} data-page={1} className="relative mx-auto shrink-0 bg-white shadow-soft" style={{ width }}>
      {/* eslint-disable-next-line @next/next/no-img-element -- a signed, short-lived URL: no image optimiser */}
      <img
        src={url}
        alt={t("pageOf", { page: 1, count: 1 })}
        referrerPolicy="no-referrer"
        className="block w-full"
        onLoad={(event) =>
          setNatural({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })
        }
      />
      {natural && <Overlays highlights={highlights} scale={scale} activeId={activeId} />}
    </div>
  );
}

function HighlightList({
  highlights,
  activeId,
  onSelect,
  subject,
}: {
  highlights: readonly Highlight[];
  activeId: string | null;
  onSelect: (h: Highlight) => void;
  subject: Subject;
}) {
  const t = useTranslations("Viewer");
  const explain = useExplanation();
  const severity = useTranslations("Severity");
  const guardrail = useTranslations("Guardrail");
  const subjects = useTranslations("Explain");
  return (
    <ol className="space-y-2" aria-label={t("listLabel")}>
      {highlights.map((h) => {
        const located = h.page !== null && h.boxes.length > 0;
        const active = h.id === activeId;
        return (
          <li key={h.id}>
            <button
              type="button"
              onClick={() => onSelect(h)}
              aria-pressed={active}
              className={cn(
                "group w-full rounded-lg border bg-surface p-3 text-start text-sm transition-colors hover:bg-accent",
                active && "border-ring ring-2 ring-ring/30",
              )}
            >
              <span className="flex items-center gap-2">
                {h.kind === "evidence" ? (
                  <ScanSearch className="size-4 shrink-0 text-primary" aria-hidden />
                ) : (
                  <ShieldAlert className="size-4 shrink-0 text-danger-ink" aria-hidden />
                )}
                <span className="min-w-0 flex-1 truncate font-medium">
                  {h.kind === "evidence" ? humanizeKey(h.label) || t("evidence") : t("guardrailFinding")}
                </span>
                {h.severity && (
                  <Badge variant="outline" className="shrink-0">
                    {severity.has(h.severity as "high") ? severity(h.severity as "high") : h.severity}
                  </Badge>
                )}
              </span>
              {h.kind === "guardrail" ? (
                <>
                  <span className="mt-1.5 block text-muted-foreground">
                    {h.quote
                      ? explain(h.explanationKey ?? "guardrails.unknown.unknown", { evidence: h.quote, subject })
                      : guardrail("withheld", { subject: subjects(`subjects.${subject}`) })}
                  </span>
                  {h.removed !== undefined && (
                    <span className="mt-1.5 block text-muted-foreground">
                      {h.removed ? guardrail("removed") : guardrail("kept")}
                    </span>
                  )}
                </>
              ) : (
                h.quote && (
                  <q className="mt-1.5 block break-words text-muted-foreground">
                    <bdi>{h.quote}</bdi>
                  </q>
                )
              )}
              <span className="mt-1.5 block text-xs text-muted-foreground">
                {located ? t("onPage", { page: h.page ?? 1 }) : t("noPosition")}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

export type { Highlight } from "@/lib/evidence";
