// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Smoke test of the Next.js quickstart against a fake Pairly API: the page lists the
 * approvals waiting with their "decide in Pairly" link, and the edge webhook route accepts
 * a webhook signed exactly as Pairly signs it and refuses a bad one. The Express one:
 * ../express/server.test.ts; the Python ones: scripts/tests/test_quickstarts.py.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildEvent, signedDelivery } from "@pairly/sdk";

const KEY = `pk_test_abcd1234_${"A".repeat(43)}`;
const SECRET = `whsec_${"q".repeat(40)}`;
const APPROVAL = "8b1d0f5e-2c4a-4e3b-9d7f-0a1b2c3d4e5f";
const WEB = "http://localhost:3100";

async function fakeApi(request: Request): Promise<Response> {
  expect(new URL(request.url).pathname).toBe("/v1/approvals");
  return Response.json({
    items: [{ id: APPROVAL, title: "Invoice INV-1", sla_state: "on_track" }],
    next_cursor: null,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Next.js quickstart", () => {
  it("verifies webhooks on the edge route", async () => {
    vi.stubEnv("PAIRLY_WEBHOOK_SECRET", SECRET);
    const { POST, runtime } = await import("./app/api/pairly/webhook/route.ts");
    expect(runtime).toBe("edge");
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
    const { body, headers } = await signedDelivery(event, SECRET);
    const url = "http://app.test/api/pairly/webhook";
    const ok = await POST(new Request(url, { method: "POST", body, headers }));
    expect(ok.status).toBe(204);
    const bad = await POST(new Request(url, { method: "POST", body: `${body} `, headers }));
    expect(bad.status).toBe(400);
  });

  it("renders the approvals waiting with their links", async () => {
    vi.stubEnv("PAIRLY_API_KEY", KEY);
    vi.stubEnv("PAIRLY_WEB_URL", WEB);
    vi.stubGlobal("fetch", fakeApi);
    const { default: Home } = await import("./app/page.tsx");
    const page = await Home({ searchParams: Promise.resolve({ job: "job-1", entity: "inv-1" }) });
    const html = renderToStaticMarkup(page);
    expect(html).toContain(`${WEB}/en/inbox/${APPROVAL}`);
    expect(html).toContain("Submitted: job job-1");
  });
});
