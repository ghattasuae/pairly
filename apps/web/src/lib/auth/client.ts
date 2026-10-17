// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Browser auth client. Same-origin: it talks to /api/auth on this web app. No organization
 * client: organisations come from the API and switch through a server action.
 */
import { magicLinkClient, twoFactorClient } from "better-auth/client/plugins";
import { createAuthClient } from "better-auth/react";

export const authClient = createAuthClient({
  basePath: "/api/auth",
  plugins: [magicLinkClient(), twoFactorClient()],
});
