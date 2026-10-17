// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

import { describe, expect, it } from "vitest";

import { PairlyAPIError, PairlyConfigError, PairlyConnectionError } from "./errors.js";
import { Pairly } from "./client.js";
import { retryDelay } from "./http.js";

const KEY = `pk_test_abcd1234_${"A".repeat(43)}`;
const BATCH_ID = "5f0c2b4e-8a1d-4c3e-9f2a-1b2c3d4e5f60";

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function problem(status: number, code: string, headers: Record<string, string> = {}): Response {
  return json(status, { type: "about:blank", title: "t", status, detail: "d", code }, headers);
}

function batch(status = "completed") {
  return {
    id: BATCH_ID,
    workflow: "pay",
    status,
    item_count: 1,
    metadata: {},
    counts: { accepted: 1 },
    items: [
      {
        id: "i",
        position: 0,
        source: "file",
        url: null,
        filename: "inv.pdf",
        status: "accepted",
        reason: null,
        document_id: "d1",
        entity_type: "pay.invoice",
        entity_id: "inv-1",
        completed_at: null,
      },
    ],
    created_at: "2026-10-16T00:00:00Z",
    completed_at: null,
  };
}

/** A fake API: answers from a list (the last one repeats) and records each request. */
function script(...responses: Array<Response | Error>) {
  const requests: Request[] = [];
  const bodies: string[] = [];
  const queue = [...responses];
  const fetch = async (request: Request): Promise<Response> => {
    requests.push(request);
    bodies.push(await request.clone().text());
    const next = queue.length > 1 ? queue.shift() : queue[0];
    if (next instanceof Error) throw next;
    return (next as Response).clone();
  };
  return { fetch, requests, bodies };
}

function client(fake: ReturnType<typeof script>, extra: Partial<ConstructorParameters<typeof Pairly>[0]> = {}) {
  const slept: number[] = [];
  const pairly = new Pairly({
    apiKey: KEY,
    fetch: fake.fetch,
    retry: { jitter: () => 1 },
    sleep: async (ms) => {
      slept.push(ms);
    },
    ...extra,
  });
  return { pairly, slept };
}

describe("Pairly client", () => {
  it("retries 429 and 5xx with backoff and keeps one Idempotency-Key", async () => {
    const fake = script(problem(429, "rate_limited", { "retry-after": "2" }), problem(503, "x"), json(202, batch()));
    const { pairly, slept } = client(fake);
    const job = await pairly.documents.submit(new Uint8Array([37, 80, 68, 70]), {
      workflow: "pay",
      filename: "inv.pdf",
    });
    expect(job.jobId).toBe(BATCH_ID);
    expect(job.entityType).toBe("pay.invoice");
    expect(job.entityId).toBe("inv-1");
    expect(slept).toEqual([2000, 1000]);
    expect(fake.requests).toHaveLength(3);
    const keys = new Set(fake.requests.map((r) => r.headers.get("idempotency-key")));
    expect(keys.size).toBe(1);
    const first = fake.requests[0]!;
    expect(new URL(first.url).pathname).toBe("/v1/documents/batch");
    expect(new URL(first.url).searchParams.get("workflow")).toBe("pay");
    expect(first.headers.get("authorization")).toBe(`Bearer ${KEY}`);
    expect(first.headers.get("content-type")).toMatch(/^multipart\/form-data; boundary=/);
    expect(fake.bodies[2]).toContain('name="file"; filename="inv.pdf"');
  });

  it("stops retrying and throws the problem", async () => {
    const fake = script(problem(429, "rate_limited", { "retry-after": "1" }));
    const { pairly, slept } = client(fake, { maxRetries: 2 });
    const error = await pairly.approvals.list().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(PairlyAPIError);
    expect((error as PairlyAPIError).status).toBe(429);
    expect((error as PairlyAPIError).code).toBe("rate_limited");
    expect((error as PairlyAPIError).retryAfterMs).toBe(1000);
    expect(fake.requests).toHaveLength(3);
    expect(slept).toEqual([1000, 1000]);
  });

  it("does not retry client errors", async () => {
    for (const [status, code] of [
      [401, "invalid_api_key"],
      [403, "api_key_action_not_allowed"],
      [404, "not_found"],
    ] as const) {
      const fake = script(problem(status, code));
      const { pairly, slept } = client(fake);
      const error = await pairly.approvals.get(BATCH_ID).catch((e: unknown) => e);
      expect((error as PairlyAPIError).status).toBe(status);
      expect((error as PairlyAPIError).code).toBe(code);
      expect(fake.requests).toHaveLength(1);
      expect(slept).toEqual([]);
    }
  });

  it("retries network errors, then gives up", async () => {
    const fake = script(new TypeError("fetch failed"));
    const { pairly, slept } = client(fake, { maxRetries: 2 });
    await expect(pairly.approvals.list()).rejects.toBeInstanceOf(PairlyConnectionError);
    expect(fake.requests).toHaveLength(3);
    expect(slept).toEqual([500, 1000]);
  });

  it("caps and jitters the backoff", () => {
    const policy = { maxRetries: 3, baseDelayMs: 500, maxDelayMs: 4000, jitter: () => 0.5 };
    expect([1, 2, 3, 4, 5].map((n) => retryDelay(policy, n, null))).toEqual([250, 500, 1000, 2000, 2000]);
    expect(retryDelay(policy, 1, 600_000)).toBe(60_000);
  });

  it("listMine follows every page", async () => {
    const fake = script(
      json(200, { items: [{ id: "a" }, { id: "b" }], next_cursor: "c1" }),
      json(200, { items: [{ id: "c" }], next_cursor: null }),
    );
    const { pairly } = client(fake);
    const mine = await pairly.approvals.listMine({ workflow: "pay" });
    expect(mine.map((a) => a.id)).toEqual(["a", "b", "c"]);
    const first = new URL(fake.requests[0]!.url).searchParams;
    expect(first.get("assigned_to_me")).toBe("true");
    expect(first.get("status")).toBe("pending");
    expect(first.get("workflow")).toBe("pay");
    expect(new URL(fake.requests[1]!.url).searchParams.get("cursor")).toBe("c1");
  });

  it("sends URL batches as JSON and proposals as notes or actions", async () => {
    const fake = script(json(202, batch("processing")), json(201, { id: "p" }));
    const { pairly } = client(fake);
    await pairly.documents.submitBatch({ workflow: "pay", urls: ["https://files.example.com/a.pdf"], metadata: { run: 1 } });
    expect(JSON.parse(fake.bodies[0]!)).toEqual({ urls: ["https://files.example.com/a.pdf"], metadata: { run: 1 } });
    await pairly.proposals.create({ entityType: "approval", entityId: BATCH_ID, body: "ok", action: "approve" });
    expect(JSON.parse(fake.bodies[1]!)).toEqual({
      entity_type: "approval",
      entity_id: BATCH_ID,
      kind: "action",
      body: "ok",
      action: "approve",
    });
    expect(fake.requests[1]!.headers.get("idempotency-key")).toMatch(/^ts-[0-9a-f]{32}$/);
  });

  it("waits for a batch to complete", async () => {
    const fake = script(json(200, batch("processing")), json(200, batch()));
    const { pairly, slept } = client(fake);
    const done = await pairly.documents.waitForBatch(BATCH_ID, { intervalMs: 250 });
    expect(done.status).toBe("completed");
    expect(slept).toEqual([250]);
  });

  it("checks ids before building a URL", async () => {
    const fake = script(json(200, {}));
    const { pairly } = client(fake);
    await expect(pairly.approvals.get("../x/decision")).rejects.toThrow("UUID");
    await expect(pairly.decisions.receipt("dec_../../x")).rejects.toThrow("dec_");
    await expect(pairly.request("GET", "https://evil.example/v1/x")).rejects.toThrow("/v1/");
    expect(fake.requests).toHaveLength(0);
  });

  it("fails closed on bad configuration", () => {
    const bad: Array<[ConstructorParameters<typeof Pairly>[0], string]> = [
      [{}, "exactly one"],
      [{ apiKey: "sk_live_x" }, "Pairly API key"],
      [{ token: "a.b.c" }, "orgId"],
      [{ apiKey: KEY, orgId: "acme" }, "UUID"],
      [{ apiKey: KEY, baseUrl: "http://api.example.com" }, "https"],
      [{ apiKey: KEY, baseUrl: "ftp://x" }, "http"],
    ];
    for (const [options, message] of bad) {
      expect(() => new Pairly(options)).toThrow(PairlyConfigError);
      expect(() => new Pairly(options)).toThrow(message);
    }
    const ok = new Pairly({ token: "a.b.c", orgId: BATCH_ID, baseUrl: "https://api.example.com/" });
    expect(ok.config.baseUrl).toBe("https://api.example.com");
    expect(ok.config.headers["X-Pairly-Org"]).toBe(BATCH_ID);
  });
});
