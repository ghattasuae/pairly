// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

/**
 * Smoke test of the Express quickstart against a fake Pairly API: the approvals waiting
 * are listed with their "decide in Pairly" link, a file goes to POST /v1/documents/batch,
 * and a webhook signed exactly as Pairly signs it is accepted while a bad one is refused.
 * The Next.js one: ../nextjs/app.test.ts; the Python ones: scripts/tests/test_quickstarts.py.
 */

import type { AddressInfo } from "node:net";

import { afterEach, describe, expect, it, vi } from "vitest";
import { Pairly, buildEvent, signedDelivery } from "@pairly/sdk";

import { createApp } from "./server.ts";

const KEY = `pk_test_abcd1234_${"A".repeat(43)}`;
const SECRET = `whsec_${"q".repeat(40)}`;
const APPROVAL = "8b1d0f5e-2c4a-4e3b-9d7f-0a1b2c3d4e5f";
const WEB = "http://localhost:3100";

async function fakeApi(request: Request): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname === "/v1/approvals") {
    return Response.json({
      items: [{ id: APPROVAL, title: "Invoice INV-1", sla_state: "on_track" }],
      next_cursor: null,
    });
  }
  expect(url.pathname).toBe("/v1/documents/batch");
  expect(url.searchParams.get("workflow")).toBe("pay");
  expect(await request.text()).toContain('filename="inv.pdf"');
  return Response.json(
    {
      id: "job-1",
      status: "completed",
      items: [{ status: "accepted", document_id: "d1", entity_type: "pay.invoice", entity_id: "inv-1" }],
    },
    { status: 202 },
  );
}

async function decided(): Promise<{ body: string; headers: Record<string, string> }> {
  const event = buildEvent(
    "approval.decided",
    {
      approval_id: APPROVAL,
      run_id: "r",
      workflow: "pay",
      approval_type: "pay.invoice",
      step: "manager",
      decision_id: "d",
      decision: "approve",
      approval_status: "approved",
      entity_type: "pay.invoice",
      entity_id: "inv-1",
    },
    { orgId: "o" },
  );
  return signedDelivery(event, SECRET);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Express quickstart", () => {
  it("lists approvals, submits a file and verifies webhooks", async () => {
    const pairly = new Pairly({ apiKey: KEY, fetch: fakeApi });
    const server = createApp(pairly, { webhookSecret: SECRET, webUrl: WEB }).listen(0);
    try {
      const { port } = server.address() as AddressInfo;
      const base = `http://127.0.0.1:${port}`;
      const home = await (await fetch(`${base}/`)).text();
      expect(home).toContain(`${WEB}/en/inbox/${APPROVAL}"`);
      const sent = await fetch(`${base}/submit?name=inv.pdf`, { method: "POST", body: new Uint8Array([37, 80, 68, 70]) });
      expect(await sent.json()).toMatchObject({ jobId: "job-1", entity: "pay.invoice inv-1" });
      const { body, headers } = await decided();
      expect((await fetch(`${base}/webhooks/pairly`, { method: "POST", body, headers })).status).toBe(204);
      const signature = headers["Pairly-Signature"] ?? "";
      const flipped = `${signature.slice(0, -1)}${signature.endsWith("0") ? "1" : "0"}`;
      const bad = { ...headers, "Pairly-Signature": flipped };
      expect((await fetch(`${base}/webhooks/pairly`, { method: "POST", body, headers: bad })).status).toBe(400);
      const events = (await (await fetch(`${base}/events`)).json()) as Array<Record<string, unknown>>;
      expect(events[0]).toMatchObject({ approval: `${WEB}/en/inbox/${APPROVAL}`, decision: "approve" });
    } finally {
      server.close();
    }
  });
});
