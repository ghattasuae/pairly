// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import Link from "next/link";

import { fontVariables } from "./fonts";

// Requests outside a locale that the proxy did not redirect (rare). English, no providers.
export default function GlobalNotFound() {
  return (
    <html lang="en" dir="ltr" className={fontVariables}>
      <body className="bg-ledger flex min-h-dvh items-center justify-center p-6">
        <main className="max-w-md space-y-3">
          <p className="font-mono text-sm text-muted-foreground">404</p>
          <h1 className="font-display text-4xl">Page not found</h1>
          <p className="text-muted-foreground">The page you asked for does not exist or has moved.</p>
          <Link className="text-sm font-medium text-primary underline-offset-4 hover:underline" href="/">
            Back to home
          </Link>
        </main>
      </body>
    </html>
  );
}
