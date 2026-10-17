// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * A minimal App Router for stories. The App Router wrapper of @storybook/nextjs-vite 10.6
 * (`nextjs.appDirectory: true`) crashes under Next 16.4 (its LayoutRouterContext value no
 * longer matches), and 10.6.1 is the newest stable release. So stories run with
 * `appDirectory: false` and this decorator provides only what `next/navigation` hooks
 * read: the router (`useRouter`, a recording stand-in), the pathname, search params and
 * route params. No layout router, so nothing here depends on Next's internal cache shape.
 *
 * Per story: `parameters.nextjs.navigation = { pathname, query, segments }` as in
 * Storybook's own API (`segments` as `[["locale", "en"]]` or `{ locale: "en" }`).
 */
import type { Decorator } from "@storybook/nextjs-vite";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime.js";
import {
  PathnameContext,
  PathParamsContext,
  SearchParamsContext,
} from "next/dist/shared/lib/hooks-client-context.shared-runtime.js";
import { useMemo } from "react";
import type * as React from "react";
import { fn } from "storybook/test";

type Segments = [string, string][] | Record<string, string>;

interface NavigationParams {
  pathname?: string;
  query?: Record<string, string>;
  segments?: Segments;
}

function paramsOf(segments: Segments | undefined): Record<string, string> {
  if (!segments) return {};
  if (Array.isArray(segments)) {
    return Object.fromEntries(segments.filter((s) => Array.isArray(s) && s.length === 2 && typeof s[0] === "string"));
  }
  return { ...segments };
}

const router = {
  push: fn().mockName("useRouter().push"),
  replace: fn().mockName("useRouter().replace"),
  back: fn().mockName("useRouter().back"),
  forward: fn().mockName("useRouter().forward"),
  refresh: fn().mockName("useRouter().refresh"),
  prefetch: fn().mockName("useRouter().prefetch"),
  hmrRefresh: fn().mockName("useRouter().hmrRefresh"),
};

function StoryRouter({ navigation, children }: { navigation: NavigationParams; children: React.ReactNode }) {
  const pathname = navigation.pathname ?? "/en";
  const query = navigation.query;
  const search = useMemo(() => new URLSearchParams(query ?? {}), [query]);
  const params = useMemo(() => paramsOf(navigation.segments ?? [["locale", pathname.split("/")[1] || "en"]]), [navigation.segments, pathname]);
  return (
    <AppRouterContext.Provider value={router as never}>
      <PathnameContext.Provider value={pathname}>
        <SearchParamsContext.Provider value={search as never}>
          <PathParamsContext.Provider value={params}>{children}</PathParamsContext.Provider>
        </SearchParamsContext.Provider>
      </PathnameContext.Provider>
    </AppRouterContext.Provider>
  );
}

export const withAppRouter: Decorator = (Story, { parameters }) => (
  <StoryRouter navigation={(parameters.nextjs?.navigation as NavigationParams | undefined) ?? {}}>
    <Story />
  </StoryRouter>
);
