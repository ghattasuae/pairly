// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";
import { vi } from "vitest";

/** Stand-in for @/i18n/navigation: plain anchors and spy routers, no Next runtime. */
export const routerSpies = { push: vi.fn(), replace: vi.fn(), refresh: vi.fn() };
export let currentPath = "/";
export function setPath(path: string) {
  currentPath = path;
}

export const navigationMock = {
  Link: ({ href, children, ...rest }: { href: string; children: React.ReactNode } & Record<string, unknown>) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
  usePathname: () => currentPath,
  useRouter: () => routerSpies,
  redirect: vi.fn(),
  getPathname: ({ href }: { href: string }) => href,
};
