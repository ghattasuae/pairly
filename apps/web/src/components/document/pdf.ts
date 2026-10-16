// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * PDF.js (pdfjs-dist, Apache-2.0), loaded on demand so pages without a document never
 * download it. Its worker and standard fonts are served from our own origin
 * (`/pdfjs/`, copied there by scripts/copy-pdfjs.mjs), which the Content Security Policy
 * allows (`worker-src 'self'`). Scripts inside PDFs never run (no scripting, no XFA).
 */
import type * as PdfJs from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";

export interface OpenedPdf {
  doc: PDFDocumentProxy;
  /** Stop the worker side and free memory. */
  destroy: () => Promise<void>;
}

export const PDFJS_BASE = "/pdfjs/";

let loader: Promise<typeof PdfJs> | null = null;

function pdfjs() {
  loader ??= import("pdfjs-dist").then((lib) => {
    lib.GlobalWorkerOptions.workerSrc = `${PDFJS_BASE}pdf.worker.min.mjs`;
    return lib;
  });
  return loader;
}

/** Open a PDF from bytes (fetched once through the signed URL, which expires quickly). */
export async function openPdf(data: ArrayBuffer): Promise<OpenedPdf> {
  const lib = await pdfjs();
  const task = lib.getDocument({
    data: new Uint8Array(data),
    enableXfa: false,
    disableAutoFetch: true,
    disableStream: true,
    disableRange: true,
    standardFontDataUrl: `${PDFJS_BASE}standard_fonts/`,
    useSystemFonts: false,
  });
  return { doc: await task.promise, destroy: () => task.destroy() };
}
