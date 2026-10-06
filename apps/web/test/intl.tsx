// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { render, type RenderOptions } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type * as React from "react";

import ar from "../messages/ar.json";
import en from "../messages/en.json";

/** Render inside next-intl with the real message files. */
export function renderWithIntl(ui: React.ReactElement, locale: "en" | "ar" = "en", options?: RenderOptions) {
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "ar" ? ar : en} timeZone="UTC">
      {ui}
    </NextIntlClientProvider>,
    options,
  );
}
