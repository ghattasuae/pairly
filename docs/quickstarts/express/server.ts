// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

/**
 * Add human-approved AI to an Express app in 10 minutes (Pairly quickstart).
 *
 *   npm install @pairly/sdk express
 *   export PAIRLY_API_KEY=pk_test_...            # make sandbox prints one
 *   export PAIRLY_API_URL=http://127.0.0.1:8000  # the native stack
 *   export PAIRLY_WEB_URL=http://localhost:3100  # where people decide
 *   export PAIRLY_WEBHOOK_SECRET=whsec_...       # from Settings, Webhooks
 *   PORT=5003 npm start
 *
 * - GET  /                 the approvals waiting, and a file picker
 * - POST /submit?name=...  the file's bytes go to Pairly (documents.submit)
 * - POST /webhooks/pairly  Pairly's signed webhooks (raw body, verified first)
 * - GET  /events           what the webhooks said
 *
 * Pairly's AI reads and checks the invoice; a person approves it in Pairly (the link is
 * on the page). This app never approves anything itself.
 */

import { pathToFileURL } from "node:url";

import express, { type Express } from "express";
import { Pairly, WebhookSignatureError, constructEvent } from "@pairly/sdk";

export interface QuickstartOptions {
  webhookSecret?: string;
  webUrl?: string;
}

const escape = (text: string): string =>
  text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

export function createApp(pairly?: Pairly, options: QuickstartOptions = {}): Express {
  const app = express();
  const events: Array<Record<string, unknown>> = [];
  const secret = options.webhookSecret ?? process.env["PAIRLY_WEBHOOK_SECRET"] ?? "";
  const web = (options.webUrl ?? process.env["PAIRLY_WEB_URL"] ?? "http://localhost:3100").replace(/\/+$/, "");
  let client = pairly;
  const api = (): Pairly => {
    client ??= new Pairly({
      apiKey: process.env["PAIRLY_API_KEY"] ?? "",
      baseUrl: process.env["PAIRLY_API_URL"] ?? "http://127.0.0.1:8000",
    });
    return client;
  };

  app.get("/", async (_req, res) => {
    const waiting = await api().approvals.listMine();
    const rows = waiting
      .map((a) => `<li>${escape(a.title)} (${a.sla_state ?? "?"}) <a href="${web}/en/inbox/${a.id}">decide in Pairly</a></li>`)
      .join("");
    res.type("html").send(
      `<h1>Submit an invoice</h1><input type="file" id="f"> <button onclick="send()">Submit</button><pre id="out"></pre>` +
        `<script>async function send(){const f=document.getElementById('f').files[0];` +
        `const r=await fetch('/submit?name='+encodeURIComponent(f.name),{method:'POST',body:f});` +
        `document.getElementById('out').textContent=await r.text();}</script>` +
        `<h2>Waiting for a person (${waiting.length})</h2><ul>${rows}</ul>`,
    );
  });

  app.post("/submit", express.raw({ type: () => true, limit: "20mb" }), async (req, res) => {
    const name = typeof req.query["name"] === "string" ? req.query["name"] : "invoice.pdf";
    const job = await api().documents.submit(new Uint8Array(req.body as Buffer), { workflow: "pay", filename: name });
    res.json({
      jobId: job.jobId,
      status: job.status,
      entity: `${job.entityType ?? ""} ${job.entityId ?? ""}`.trim(),
      note: "The AI checks it now; a person approves it in Pairly.",
      inbox: `${web}/en/inbox`,
    });
  });

  // The raw body: the signature covers the exact bytes Pairly sent.
  app.post("/webhooks/pairly", express.raw({ type: "application/json" }), async (req, res) => {
    try {
      const event = await constructEvent(new Uint8Array(req.body as Buffer), req.get("Pairly-Signature"), secret);
      const summary: Record<string, unknown> = { id: event.id, type: event.type };
      if (event.type === "approval.decided") {
        summary["approval"] = `${web}/en/inbox/${event.data.approval_id}`;
        summary["decision"] = event.data.decision;
      } else if (event.type === "batch.completed") {
        summary["counts"] = event.data.counts;
      }
      events.unshift(summary);
      events.length = Math.min(events.length, 50);
      res.status(204).end();
    } catch (error) {
      if (error instanceof WebhookSignatureError) {
        res.status(400).json({ error: "bad signature" });
        return;
      }
      throw error;
    }
  });

  app.get("/events", (_req, res) => {
    res.json(events);
  });

  return app;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env["PORT"] ?? 5003);
  createApp().listen(port, () => console.log(`Pairly quickstart on http://localhost:${port}`));
}
