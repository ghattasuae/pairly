// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

import { redirect } from "next/navigation";

import { pairly } from "../lib/pairly";

/** Send the uploaded invoice to Pairly; the page then shows the job. */
export async function submitInvoice(form: FormData): Promise<void> {
  const file = form.get("file");
  if (!(file instanceof File) || file.size === 0) redirect("/?error=no-file");
  const job = await pairly().documents.submit(file, { workflow: "pay", filename: file.name });
  redirect(`/?job=${encodeURIComponent(job.jobId)}&entity=${encodeURIComponent(job.entityId ?? "")}`);
}
