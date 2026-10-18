// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

export type DownloadResult = { ok: true } | { ok: false; code: string };

/**
 * POST to one of our own download routes (which add the token server side) and save
 * the file. The `X-Pairly-Export` header proves the request came from our page. `body`,
 * when given, is sent as JSON.
 */
export async function saveDownload(url: string, fallbackName: string, body?: unknown): Promise<DownloadResult> {
  const headers: Record<string, string> = { "X-Pairly-Export": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method: "POST",
    headers,
    credentials: "same-origin",
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    let code = "unavailable";
    try {
      const body = (await response.json()) as { code?: unknown };
      if (typeof body.code === "string") code = body.code;
    } catch {
      /* not JSON */
    }
    return { ok: false, code };
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? fallbackName;
  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 1000);
  return { ok: true };
}
