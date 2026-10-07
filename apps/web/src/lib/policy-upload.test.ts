// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import {
  checkUploadRequest,
  MAX_FILE_BYTES,
  MAX_REQUEST_BYTES,
  parsePolicyUploadForm,
  readCapped,
  UPLOAD_HEADER,
} from "./policy-upload";

const ORIGINS = ["http://localhost:3100"];

function headers(values: Record<string, string>): Headers {
  return new Headers({
    "sec-fetch-site": "same-origin",
    origin: "http://localhost:3100",
    [UPLOAD_HEADER]: "1",
    "content-type": "multipart/form-data; boundary=x",
    "content-length": "1000",
    ...values,
  });
}

describe("checkUploadRequest", () => {
  it("accepts a same-origin multipart request with the custom header", () => {
    expect(checkUploadRequest(headers({}), ORIGINS)).toBeNull();
  });

  it("refuses cross-site requests and requests without the custom header", () => {
    expect(checkUploadRequest(headers({ "sec-fetch-site": "cross-site" }), ORIGINS)?.code).toBe("cross_site");
    expect(checkUploadRequest(headers({ "sec-fetch-site": "same-site" }), ORIGINS)?.code).toBe("cross_site");
    expect(checkUploadRequest(headers({ origin: "https://evil.example" }), ORIGINS)?.code).toBe("cross_site");
    const noHeader = headers({});
    noHeader.delete(UPLOAD_HEADER);
    expect(checkUploadRequest(noHeader, ORIGINS)?.status).toBe(403);
    const anonymous = headers({});
    anonymous.delete("sec-fetch-site");
    anonymous.delete("origin");
    expect(checkUploadRequest(anonymous, ORIGINS)?.code).toBe("cross_site");
    // An older browser without Sec-Fetch-Site still sends a trusted Origin.
    const older = headers({});
    older.delete("sec-fetch-site");
    expect(checkUploadRequest(older, ORIGINS)).toBeNull();
  });

  it("refuses other bodies and declared lengths over 26 MB", () => {
    expect(checkUploadRequest(headers({ "content-type": "application/json" }), ORIGINS)?.status).toBe(415);
    expect(checkUploadRequest(headers({ "content-length": String(MAX_REQUEST_BYTES + 1) }), ORIGINS)).toEqual({
      status: 413,
      code: "too_large",
    });
    expect(checkUploadRequest(headers({ "content-length": "-1" }), ORIGINS)?.status).toBe(400);
    expect(MAX_REQUEST_BYTES).toBe(26 * 1024 * 1024);
  });
});

function stream(chunks: number[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const size of chunks) controller.enqueue(new Uint8Array(size).fill(65));
      controller.close();
    },
  });
}

describe("readCapped", () => {
  it("reads a body within the limit and refuses one past it", async () => {
    expect((await readCapped(stream([3, 4]), 10))?.byteLength).toBe(7);
    expect(await readCapped(stream([6, 6]), 10)).toBeNull();
    expect((await readCapped(null, 10))?.byteLength).toBe(0);
  });
});

describe("parsePolicyUploadForm", () => {
  const form = (values: Record<string, string | File>) => {
    const data = new FormData();
    for (const [key, value] of Object.entries(values)) data.set(key, value);
    return data;
  };
  const file = new File(["policy text"], "policy.pdf", { type: "application/pdf" });

  it("returns the checked fields", () => {
    const parsed = parsePolicyUploadForm(form({ name: "travel", title: "  Travel policy ", file }));
    expect(parsed?.name).toBe("travel");
    expect(parsed?.title).toBe("Travel policy");
  });

  it("refuses bad names, empty titles, empty or oversized files", () => {
    expect(parsePolicyUploadForm(form({ name: "Travel!", title: "T", file }))).toBeNull();
    expect(parsePolicyUploadForm(form({ name: "travel", title: " ", file }))).toBeNull();
    expect(parsePolicyUploadForm(form({ name: "travel", title: "T", file: new File([], "e.pdf") }))).toBeNull();
    const big = new File([new Uint8Array(MAX_FILE_BYTES + 1)], "big.pdf");
    expect(parsePolicyUploadForm(form({ name: "travel", title: "T", file: big }))).toBeNull();
  });
});
