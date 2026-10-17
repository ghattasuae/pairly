// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { SearchHit } from "./api/types";

/** Where a search hit opens: a document in the viewer, a policy on the policies page. */
export function hitHref(hit: Pick<SearchHit, "kind" | "source_id" | "page_start">): string | null {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(hit.source_id)) return null;
  if (hit.kind === "document") return `/documents/${hit.source_id}${hit.page_start ? `?page=${hit.page_start}` : ""}`;
  if (hit.kind === "policy") return `/settings/policies#policy-${hit.source_id}`;
  return null;
}
