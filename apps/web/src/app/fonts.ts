// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Typefaces, all under the SIL Open Font License 1.1 and committed under src/fonts/ (each
 * folder holds its OFL.txt, NOTICE.md lists them). next/font/local serves them from the
 * app itself, so no request ever leaves for a font CDN, and the offline demo looks the same.
 *
 * - IBM Plex Sans: body and UI (400, 500, 600; the Latin-1 subset IBM ships).
 * - IBM Plex Sans Arabic: Arabic UI in the same weights, so both scripts read as one
 *   family. Limited to the Arabic ranges with unicode-range and never preloaded, so
 *   English pages do not download it.
 * - Fraunces (variable: optical size, weight, softness): display headings and big numbers.
 *   Arabic headings fall through to Plex Sans Arabic, set heavier in globals.css.
 * - IBM Plex Mono: ids, hashes, amounts, code and keyboard hints (400, 500).
 */
import localFont from "next/font/local";

export const fontUi = localFont({
  src: [
    { path: "../fonts/ibm-plex-sans/IBMPlexSans-Regular-Latin1.woff2", weight: "400", style: "normal" },
    { path: "../fonts/ibm-plex-sans/IBMPlexSans-Medium-Latin1.woff2", weight: "500", style: "normal" },
    { path: "../fonts/ibm-plex-sans/IBMPlexSans-SemiBold-Latin1.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-ui",
  display: "swap",
  // No fallback in the variable: globals.css puts Plex Sans Arabic next in every stack,
  // and a metric fallback (Arial) would catch the Arabic glyphs first.
  adjustFontFallback: false,
});

export const fontArabic = localFont({
  src: [
    { path: "../fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-Regular.woff2", weight: "400", style: "normal" },
    { path: "../fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-Medium.woff2", weight: "500", style: "normal" },
    { path: "../fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-SemiBold.woff2", weight: "600", style: "normal" },
    { path: "../fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-Bold.woff2", weight: "700", style: "normal" },
  ],
  variable: "--font-arabic",
  display: "swap",
  preload: false,
  // Arabic, Arabic Supplement and Extended, presentation forms, joiners and marks.
  // (next/font needs a literal here.)
  declarations: [
    {
      prop: "unicode-range",
      value: "U+0600-06FF, U+0750-077F, U+0870-089F, U+08A0-08FF, U+200C-200F, U+FB50-FDFF, U+FE70-FEFF",
    },
  ],
  adjustFontFallback: false,
});

export const fontSerif = localFont({
  src: [{ path: "../fonts/fraunces/Fraunces-latin-basic.woff2", weight: "100 900", style: "normal" }],
  variable: "--font-serif",
  display: "swap",
  adjustFontFallback: false,
});

export const fontCode = localFont({
  src: [
    { path: "../fonts/ibm-plex-mono/IBMPlexMono-Regular-Latin1.woff2", weight: "400", style: "normal" },
    { path: "../fonts/ibm-plex-mono/IBMPlexMono-Medium-Latin1.woff2", weight: "500", style: "normal" },
  ],
  variable: "--font-code",
  display: "swap",
  adjustFontFallback: false,
});

export const fontVariables = [fontUi, fontArabic, fontSerif, fontCode]
  .map((font) => font.variable)
  .join(" ");
