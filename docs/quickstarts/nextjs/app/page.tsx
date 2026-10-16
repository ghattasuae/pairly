// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Add human-approved AI to a Next.js app in 10 minutes (Pairly quickstart).
 *
 *   export PAIRLY_API_KEY=pk_test_...            # make sandbox prints one
 *   export PAIRLY_API_URL=http://127.0.0.1:8000  # the native stack
 *   export PAIRLY_WEB_URL=http://localhost:3100  # where people decide
 *   export PAIRLY_WEBHOOK_SECRET=whsec_...       # from Settings, Webhooks
 *   pnpm --filter @pairly/quickstart-nextjs dev  # http://localhost:5004
 *
 * This page submits an invoice (a server action) and lists the approvals waiting for a
 * person, each with its "decide in Pairly" link. app/api/pairly/webhook/route.ts
 * receives the webhooks. The app never approves anything itself.
 */

import { submitInvoice } from "./actions";
import { pairly, webUrl } from "../lib/pairly";

export const dynamic = "force-dynamic";

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const query = await searchParams;
  const waiting = await pairly().approvals.listMine();
  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 640, margin: "2rem auto" }}>
      <h1>Submit an invoice</h1>
      <form action={submitInvoice}>
        <input type="file" name="file" required /> <button type="submit">Submit</button>
      </form>
      {typeof query["job"] === "string" && (
        <p>
          Submitted: job {query["job"]}, invoice {String(query["entity"] ?? "")}. The AI checks it now; a person
          approves it in Pairly.
        </p>
      )}
      <h2>Waiting for a person ({waiting.length})</h2>
      <ul>
        {waiting.map((approval) => (
          <li key={approval.id}>
            {approval.title} ({approval.sla_state ?? "?"}){" "}
            <a href={`${webUrl}/en/inbox/${approval.id}`}>decide in Pairly</a>
          </li>
        ))}
      </ul>
    </main>
  );
}
