// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Decorator, Preview } from "@storybook/nextjs-vite";
import { NextIntlClientProvider } from "next-intl";
import { useEffect } from "react";
import type * as React from "react";

import "../src/app/globals.css";
import { TooltipProvider } from "../src/components/ui/tooltip";
import ar from "../messages/ar.json";
import en from "../messages/en.json";

type Locale = "en" | "ar";

/**
 * Every story renders in the locale and theme picked in the toolbar: English or Arabic
 * (RTL), light or dark. Stories can pin either with `globals`.
 */
function Frame({ locale, theme, children }: { locale: Locale; theme: string; children: React.ReactNode }) {
  useEffect(() => {
    const root = document.documentElement;
    root.lang = locale;
    root.dir = locale === "ar" ? "rtl" : "ltr";
    root.classList.toggle("dark", theme === "dark");
  }, [locale, theme]);
  return (
    <NextIntlClientProvider locale={locale} messages={locale === "ar" ? ar : en} timeZone="UTC">
      <TooltipProvider delayDuration={200}>
        <div dir={locale === "ar" ? "rtl" : "ltr"} lang={locale} className="bg-background font-sans text-foreground">
          {children}
        </div>
      </TooltipProvider>
    </NextIntlClientProvider>
  );
}

const withLocaleAndTheme: Decorator = (Story, context) => (
  <Frame
    locale={(context.globals.locale as Locale | undefined) ?? "en"}
    theme={(context.globals.theme as string | undefined) ?? "light"}
  >
    <Story />
  </Frame>
);

const preview: Preview = {
  decorators: [withLocaleAndTheme],
  globalTypes: {
    locale: {
      description: "Language and direction",
      toolbar: {
        title: "Locale",
        icon: "globe",
        items: [
          { value: "en", title: "English (LTR)" },
          { value: "ar", title: "العربية (RTL)" },
        ],
        dynamicTitle: true,
      },
    },
    theme: {
      description: "Colour theme",
      toolbar: {
        title: "Theme",
        icon: "contrast",
        items: [
          { value: "light", title: "Light" },
          { value: "dark", title: "Dark" },
        ],
        dynamicTitle: true,
      },
    },
  },
  initialGlobals: { locale: "en", theme: "light" },
  parameters: {
    // The App Router wrapper of @storybook/nextjs-vite 10.6 crashes with Next 16.4
    // (LayoutRouterContext shape). next/navigation is still mocked for every story.
    nextjs: { appDirectory: false, navigation: { pathname: "/en" } },
    layout: "fullscreen",
    a11y: { test: "error" },
    controls: { matchers: { color: /(background|color)$/i } },
  },
};

export default preview;
