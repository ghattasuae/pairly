// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

// Test-only RFC 6238 (TOTP) implementation, built on RFC 4226 (HOTP), using Node's
// built-in crypto. No third-party OTP library is installed in this repo, so this
// computes 6-digit codes directly from a base32 secret (as shown in the QR / "enter
// this key by hand" box) for the e2e tests that drive Better Auth's two-factor plugin.

import { createHmac } from "node:crypto";

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/** Decode an RFC 4648 base32 string (no padding required) into raw bytes. */
export function base32Decode(input: string): Buffer {
  const clean = input.replace(/\s+/g, "").replace(/=+$/, "").toUpperCase();
  let bits = "";
  for (const char of clean) {
    const idx = BASE32_ALPHABET.indexOf(char);
    if (idx === -1) throw new Error(`Invalid base32 character: ${char}`);
    bits += idx.toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

/** RFC 4226 HOTP value for a given counter. */
function hotp(secret: Buffer, counter: number, digits: number): string {
  const counterBuf = Buffer.alloc(8);
  // Counter is a 64-bit big-endian integer; JS numbers are safe up to 2^53 which is
  // far beyond any realistic TOTP step count, so writing the low 32 bits is enough.
  counterBuf.writeUInt32BE(Math.floor(counter / 2 ** 32), 0);
  counterBuf.writeUInt32BE(counter % 2 ** 32, 4);

  const hmac = createHmac("sha1", secret).update(counterBuf).digest();
  const offset = hmac[hmac.length - 1]! & 0x0f;
  const binCode =
    ((hmac[offset]! & 0x7f) << 24) |
    ((hmac[offset + 1]! & 0xff) << 16) |
    ((hmac[offset + 2]! & 0xff) << 8) |
    (hmac[offset + 3]! & 0xff);
  return String(binCode % 10 ** digits).padStart(digits, "0");
}

export interface TotpOptions {
  digits?: number;
  period?: number;
  /** Unix time in seconds. Defaults to now. */
  at?: number;
}

/** RFC 6238 TOTP code for a base32 secret (standard 30s step, 6 digits, SHA-1). */
export function totp(base32Secret: string, options: TotpOptions = {}): string {
  const digits = options.digits ?? 6;
  const period = options.period ?? 30;
  const at = options.at ?? Math.floor(Date.now() / 1000);
  const counter = Math.floor(at / period);
  const secret = base32Decode(base32Secret);
  return hotp(secret, counter, digits);
}

/**
 * Wait, if needed, so the returned code has at least `minSecondsLeft` seconds of
 * validity left in its 30s window, to avoid flaky expiries right as we submit it.
 */
export async function totpFresh(base32Secret: string, minSecondsLeft = 5): Promise<string> {
  const period = 30;
  let now = Math.floor(Date.now() / 1000);
  const remaining = period - (now % period);
  if (remaining < minSecondsLeft) {
    await new Promise((r) => setTimeout(r, (remaining + 1) * 1000));
    now = Math.floor(Date.now() / 1000);
  }
  return totp(base32Secret, { at: now });
}
