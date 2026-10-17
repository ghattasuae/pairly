// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Typefaces (all SIL Open Font License, self-hosted by next/font at build time, so no
 * request ever goes to Google at runtime):
 *
 * - Instrument Sans: UI text (Latin).
 * - IBM Plex Sans Arabic: Arabic text, and the fallback for Arabic glyphs everywhere.
 * - Newsreader: display headings (Latin). Arabic headings fall through to Plex Sans Arabic.
 * - IBM Plex Mono: keyboard hints, ids and codes.
 */
import { IBM_Plex_Mono, IBM_Plex_Sans_Arabic, Instrument_Sans, Newsreader } from "next/font/google";

export const fontUi = Instrument_Sans({
  subsets: ["latin", "latin-ext"],
  variable: "--font-ui",
  display: "swap",
});

export const fontArabic = IBM_Plex_Sans_Arabic({
  subsets: ["arabic", "latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-arabic",
  display: "swap",
});

export const fontSerif = Newsreader({
  subsets: ["latin", "latin-ext"],
  variable: "--font-serif",
  display: "swap",
  style: ["normal", "italic"],
});

export const fontCode = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-code",
  display: "swap",
});

export const fontVariables = [fontUi, fontArabic, fontSerif, fontCode]
  .map((font) => font.variable)
  .join(" ");
