// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

import { describe, expect, it } from "vitest";

import {
  WebhookSignatureError,
  buildEvent,
  computeSignature,
  constructEvent,
  serialise,
  signPayload,
  signedDelivery,
  verifySignature,
} from "./webhooks.js";

const SECRET = "whsec_test_vector_secret_0123456789";
const NOW = 1_800_000_000;

// Produced by the engine (pairly_core.webhooks.events.serialise and
// pairly_core.webhooks.signature.sign_payload) for this exact event, so the SDK is
// checked against the server's bytes and signature, not against itself.
const ENGINE_BODY =
  '{"created_at":"2026-10-16T08:00:00Z","data":{"batch_id":"b1","counts":{"accepted":1,"failed":0},' +
  '"metadata":{"ref":"Fattura n° 7 فاتورة"},"status":"completed","workflow":"pay"},' +
  '"id":"evt_7d4f8a2e1b3c4d5e8f90a1b2c3d4e5f6","org_id":"0b8e6f1a-2c3d-4e5f-9a0b-1c2d3e4f5a6b",' +
  '"type":"batch.completed"}';
const ENGINE_HEADER = "t=1800000000,v1=60422ec6d42b3092fb991c5c0cb594490da3bbbaf5940a01962face1d3fdcf50";

describe("webhooks", () => {
  it("verifies what the engine signs, byte for byte", async () => {
    await expect(verifySignature(ENGINE_BODY, ENGINE_HEADER, SECRET, { now: NOW })).resolves.toBe(NOW);
    expect(await signPayload(SECRET, ENGINE_BODY, NOW)).toBe(ENGINE_HEADER);
    const event = buildEvent(
      "batch.completed",
      {
        batch_id: "b1",
        workflow: "pay",
        status: "completed",
        metadata: { ref: "Fattura n° 7 فاتورة" },
        counts: { failed: 0, accepted: 1 },
        items: [],
      },
      {
        orgId: "0b8e6f1a-2c3d-4e5f-9a0b-1c2d3e4f5a6b",
        eventId: "evt_7d4f8a2e1b3c4d5e8f90a1b2c3d4e5f6",
        createdAt: new Date("2026-10-16T08:00:00Z"),
      },
    );
    // The engine's vector has no items: drop them to compare the same object.
    const data: Record<string, unknown> = { ...event.data };
    delete data["items"];
    expect(serialise({ ...event, data })).toBe(ENGINE_BODY);
  });

  it("accepts bytes as well as strings", async () => {
    const bytes = new TextEncoder().encode(ENGINE_BODY);
    await expect(verifySignature(bytes, ENGINE_HEADER, SECRET, { now: NOW })).resolves.toBe(NOW);
  });

  it("refuses tampering, replays and malformed headers", async () => {
    const cases: Array<[string, string | null, number]> = [
      [`${ENGINE_BODY} `, ENGINE_HEADER, NOW],
      [ENGINE_BODY, ENGINE_HEADER, NOW + 301],
      [ENGINE_BODY, ENGINE_HEADER, NOW - 301],
      [ENGINE_BODY, ENGINE_HEADER.replace("v1=", "v0="), NOW],
      [ENGINE_BODY, "", NOW],
      [ENGINE_BODY, null, NOW],
      [ENGINE_BODY, "t=abc,v1=00", NOW],
      [ENGINE_BODY, `t=${NOW}`, NOW],
      [ENGINE_BODY, `t=${NOW},t=${NOW},v1=00`, NOW],
      [ENGINE_BODY, "x".repeat(5000), NOW],
    ];
    for (const [body, header, now] of cases) {
      await expect(verifySignature(body, header, SECRET, { now })).rejects.toBeInstanceOf(WebhookSignatureError);
    }
    await expect(verifySignature(ENGINE_BODY, ENGINE_HEADER, "", { now: NOW })).rejects.toThrow("secret");
    await expect(verifySignature(ENGINE_BODY, ENGINE_HEADER, "whsec_other", { now: NOW })).rejects.toThrow("matches");
  });

  it("accepts any v1 during a secret rotation", async () => {
    const old = await computeSignature("whsec_old", NOW, "{}");
    const current = await computeSignature(SECRET, NOW, "{}");
    await expect(verifySignature("{}", `t=${NOW},v1=${old},v1=${current}`, SECRET, { now: NOW })).resolves.toBe(NOW);
  });

  it("constructs typed events from signed deliveries", async () => {
    const event = buildEvent(
      "approval.decided",
      {
        approval_id: "a",
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
    expect(headers["Pairly-Event-Type"]).toBe("approval.decided");
    const parsed = await constructEvent(body, headers["Pairly-Signature"], SECRET);
    expect(parsed.type).toBe("approval.decided");
    if (parsed.type === "approval.decided") {
      expect(parsed.data.decision).toBe("approve");
    }
    expect(parsed.id).toMatch(/^evt_[0-9a-f]{32}$/);
    await expect(constructEvent(body, headers["Pairly-Signature"], "whsec_wrong")).rejects.toBeInstanceOf(
      WebhookSignatureError,
    );
  });
});
