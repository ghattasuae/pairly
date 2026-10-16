// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

export type DownloadResult = { ok: true } | { ok: false; code: string };

/**
 * POST to one of our own download routes (which add the token server side) and save
 * the file. The `X-Pairly-Export` header proves the request came from our page.
 */
export async function saveDownload(url: string, fallbackName: string): Promise<DownloadResult> {
  const response = await fetch(url, { method: "POST", headers: { "X-Pairly-Export": "1" }, credentials: "same-origin" });
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
