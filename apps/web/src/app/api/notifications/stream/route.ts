// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The signed-in user's notification event stream, passed through from the API
 * (`GET /v1/notifications/stream`). The browser opens it with `EventSource` on this
 * origin; the API token is minted here and never reaches the browser. The API closes the
 * stream when the token expires (a few minutes) and the browser reconnects, sending
 * `Last-Event-ID` so nothing is missed.
 */
import { isSameOriginRequest, jsonProblem, upstream } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";

export const dynamic = "force-dynamic";

const EVENT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(request: Request): Promise<Response> {
  if (!isSameOriginRequest(request)) return jsonProblem(403, "cross_site");
  const lastEventId = request.headers.get("last-event-id");
  let response: Response;
  try {
    response = await upstream("/v1/notifications/stream", {
      accept: "text/event-stream",
      signal: request.signal,
      extraHeaders: lastEventId && EVENT_ID.test(lastEventId) ? { "Last-Event-ID": lastEventId } : {},
    });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) {
    await response.body?.cancel();
    return jsonProblem(response.status === 401 || response.status === 403 ? response.status : 502, "unavailable");
  }
  return new Response(response.body, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      // no-transform: compression would buffer the stream.
      "Cache-Control": "no-cache, no-store, no-transform",
      "X-Accel-Buffering": "no",
    },
  });
}
