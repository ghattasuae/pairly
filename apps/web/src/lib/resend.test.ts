// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { afterEach, describe, expect, it, vi } from "vitest";

import { readServerEnv } from "./env";
import { ResendError, resendBody, sendWithResend } from "./resend";

const KEY = "re_placeholder_not_a_real_key";
const mail = {
  from: "Pairly <no-reply@mail.example.test>",
  to: "person@example.test",
  subject: "Sign in",
  text: "Hello",
  html: "<p>Hello</p>",
};

function reply(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

describe("sendWithResend", () => {
  it("posts the email with the key, idempotency key, reply-to and tags", async () => {
    const fetchImpl = vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => reply(200, { id: "em_1" }));
    const id = await sendWithResend(
      { ...mail, replyTo: "ap@example.test", tags: { category: "magic_link", "bad tag": "x" } },
      { apiKey: KEY, idempotencyKey: "web-1", fetchImpl },
    );
    expect(id).toBe("em_1");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe("https://api.resend.com/emails");
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe(`Bearer ${KEY}`);
    expect(headers["Idempotency-Key"]).toBe("web-1");
    expect(JSON.parse(String(init?.body))).toEqual({
      from: mail.from,
      to: ["person@example.test"],
      subject: "Sign in",
      text: "Hello",
      html: "<p>Hello</p>",
      reply_to: ["ap@example.test"],
      tags: [{ name: "category", value: "magic_link" }],
    });
  });

  it("strips line breaks from header-like fields", () => {
    const body = resendBody({ ...mail, subject: "Hi\r\nBcc: x@example.test" });
    expect(body.subject).toBe("Hi Bcc: x@example.test");
  });

  it("retries a short rate limit with the same idempotency key", async () => {
    const sleep = vi.fn(async () => undefined);
    const fetchImpl = vi
      .fn<(url: string | URL | Request, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(reply(429, { name: "rate_limit_exceeded" }, { "retry-after": "1" }))
      .mockResolvedValueOnce(reply(200, { id: "em_2" }));
    await expect(sendWithResend(mail, { apiKey: KEY, idempotencyKey: "web-2", fetchImpl, sleep })).resolves.toBe("em_2");
    expect(sleep).toHaveBeenCalledWith(1000);
    const keys = fetchImpl.mock.calls.map(([, init]) => (init?.headers as Record<string, string>)["Idempotency-Key"]);
    expect(keys).toEqual(["web-2", "web-2"]);
  });

  it("does not wait for a quota or a long rate limit", async () => {
    for (const [name, wait] of [
      ["daily_quota_exceeded", "1"],
      ["rate_limit_exceeded", "30"],
    ]) {
      const sleep = vi.fn(async () => undefined);
      const fetchImpl = vi.fn(async () => reply(429, { name }, { "retry-after": wait! }));
      const error = await sendWithResend(mail, { apiKey: KEY, idempotencyKey: "k", fetchImpl, sleep }).catch((e) => e);
      expect(error).toBeInstanceOf(ResendError);
      expect(error.transient).toBe(true);
      expect(fetchImpl).toHaveBeenCalledTimes(1);
      expect(sleep).not.toHaveBeenCalled();
    }
  });

  it.each([
    [400, "validation_error"],
    [401, "missing_api_key"],
    [403, "invalid_permission"],
    [409, "invalid_idempotent_request"],
    [422, "invalid_attachment"],
  ])("fails at once on %i %s", async (status, name) => {
    const fetchImpl = vi.fn(async () => reply(status, { name, message: "x" }));
    const error = await sendWithResend(mail, { apiKey: KEY, idempotencyKey: "k", fetchImpl }).catch((e) => e);
    expect(error).toBeInstanceOf(ResendError);
    expect(error.message).toBe(`resend_${status}_${name}`);
    expect(error.transient).toBe(false);
    expect(error.message).not.toContain(KEY);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("retries server errors, conflicts in flight and network errors at most three times", async () => {
    const sleep = vi.fn(async () => undefined);
    const fetchImpl = vi
      .fn<(url: string | URL | Request, init?: RequestInit) => Promise<Response>>()
      .mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValueOnce(reply(409, { name: "concurrent_idempotent_requests" }))
      .mockResolvedValueOnce(reply(503, { name: "service_unavailable" }));
    const error = await sendWithResend(mail, { apiKey: KEY, idempotencyKey: "k", fetchImpl, sleep }).catch((e) => e);
    expect(error).toBeInstanceOf(ResendError);
    expect(error.status).toBe(503);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });
});

describe("web email settings", () => {
  const base = {
    NODE_ENV: "production",
    BETTER_AUTH_URL: "https://web.example.test",
    BETTER_AUTH_SECRET: "x".repeat(40),
    PAIRLY_AUTH_DATABASE_URL: "postgresql://pairly_auth:pw@db.example.test:5432/pairly",
    PAIRLY_AUTH_ISSUER: "https://web.example.test",
    PAIRLY_AUTH_AUDIENCE: "https://web.example.test",
    PAIRLY_API_INTERNAL_URL: "https://api.example.test",
    PAIRLY_ENV: "production",
  };

  function withEnv(extra: Record<string, string | undefined>) {
    for (const [name, value] of Object.entries({ ...base, ...extra })) vi.stubEnv(name, value);
  }

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("refuses the resend transport without a key (fail closed)", () => {
    withEnv({ PAIRLY_EMAIL_TRANSPORT: "resend", PAIRLY_RESEND_API_KEY: undefined });
    expect(() => readServerEnv()).toThrow(/PAIRLY_RESEND_API_KEY is required/);
  });

  it("refuses something that is not a Resend key", () => {
    withEnv({ PAIRLY_EMAIL_TRANSPORT: "resend", PAIRLY_RESEND_API_KEY: "sk_live_wrong" });
    expect(() => readServerEnv()).toThrow(/not a Resend key/);
  });

  it("accepts a key and keeps the from address variable", () => {
    withEnv({
      PAIRLY_EMAIL_TRANSPORT: "resend",
      PAIRLY_RESEND_API_KEY: KEY,
      PAIRLY_SMTP_FROM: "Pairly <no-reply@mail.example.test>",
      PAIRLY_EMAIL_REPLY_TO: "ap@example.test",
    });
    const env = readServerEnv();
    expect(env.email.transport).toBe("resend");
    expect(env.resend).toEqual({ apiKey: KEY, baseUrl: "https://api.resend.com" });
    expect(env.smtp.from).toBe("Pairly <no-reply@mail.example.test>");
    expect(env.email.replyTo).toBe("ap@example.test");
  });

  it("refuses a plain-http Resend URL in production", () => {
    withEnv({ PAIRLY_EMAIL_TRANSPORT: "resend", PAIRLY_RESEND_API_KEY: KEY, PAIRLY_RESEND_BASE_URL: "http://api.resend.com" });
    expect(() => readServerEnv()).toThrow(/must use https/);
  });
});
