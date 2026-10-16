// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Pairly's webhooks (POST /api/pairly/webhook). Runs on the edge runtime: the SDK's
 * signature check uses Web Crypto only. Register this URL in Pairly (Settings, Webhooks).
 */

import { WebhookSignatureError, constructEvent } from "@pairly/sdk";

export const runtime = "edge";

const webUrl = (process.env["PAIRLY_WEB_URL"] ?? "http://localhost:3100").replace(/\/+$/, "");

export async function POST(request: Request): Promise<Response> {
  const body = await request.text(); // the raw body: verify before parsing
  try {
    const event = await constructEvent(
      body,
      request.headers.get("Pairly-Signature"),
      process.env["PAIRLY_WEBHOOK_SECRET"] ?? "",
    );
    if (event.type === "approval.decided") {
      console.log(`approval ${event.data.decision}: ${webUrl}/en/inbox/${event.data.approval_id}`);
    } else if (event.type === "batch.completed") {
      console.log(`batch ${event.data.batch_id} done`, event.data.counts);
    }
    return new Response(null, { status: 204 });
  } catch (error) {
    if (error instanceof WebhookSignatureError) {
      return Response.json({ error: "bad signature" }, { status: 400 });
    }
    throw error;
  }
}
