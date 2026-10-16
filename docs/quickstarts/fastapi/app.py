# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Add human-approved AI to a FastAPI app in 10 minutes (Pairly quickstart).

    pip install pairly fastapi uvicorn python-multipart
    export PAIRLY_API_KEY=pk_test_...            # make sandbox prints one
    export PAIRLY_API_URL=http://127.0.0.1:8000  # the native stack
    export PAIRLY_WEB_URL=http://localhost:3100  # where people decide
    export PAIRLY_WEBHOOK_SECRET=whsec_...       # from Settings, Webhooks
    uvicorn app:app --port 5001

* ``GET  /``                  a form to submit an invoice, and the approvals waiting
* ``POST /submit``            sends the file to Pairly (``documents.submit``)
* ``POST /webhooks/pairly``   receives Pairly's signed webhooks
* ``GET  /events``            what the webhooks said

Pairly's AI reads and checks the invoice; a person approves it in Pairly (the link is on
the page). Your app never approves anything itself.
"""

from __future__ import annotations

import html
import os
from collections import deque
from typing import Any

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from pairly import AsyncPairly
from pairly.webhooks import WebhookSignatureError, construct_event


def create_app(
    pairly: AsyncPairly | None = None,
    *,
    webhook_secret: str | None = None,
    web_url: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Pairly quickstart (FastAPI)")
    events: deque[dict[str, Any]] = deque(maxlen=50)
    secret = webhook_secret or os.environ.get("PAIRLY_WEBHOOK_SECRET", "")
    web = (web_url or os.environ.get("PAIRLY_WEB_URL") or "http://localhost:3100").rstrip("/")
    state: dict[str, AsyncPairly] = {}

    def client() -> AsyncPairly:
        # Created on first use, from PAIRLY_API_KEY and PAIRLY_API_URL.
        if "pairly" not in state:
            state["pairly"] = pairly or AsyncPairly()
        return state["pairly"]

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        waiting = await client().approvals.list_mine()
        rows = "".join(
            f"<li>{html.escape(a['title'])} ({a['sla_state']}) "
            f'<a href="{web}/en/inbox/{a["id"]}">decide in Pairly</a></li>'
            for a in waiting
        )
        return (
            "<h1>Submit an invoice</h1>"
            '<form method="post" action="/submit" enctype="multipart/form-data">'
            '<input type="file" name="file" required> <button>Submit</button></form>'
            f"<h2>Waiting for a person ({len(waiting)})</h2><ul>{rows}</ul>"
        )

    @app.post("/submit", response_class=HTMLResponse)
    async def submit(file: UploadFile) -> str:
        job = await client().documents.submit(
            await file.read(), workflow="pay", filename=file.filename or "invoice.pdf"
        )
        return (
            f"<p>Submitted: job {html.escape(job.job_id)}, {html.escape(job.status)}.</p>"
            f"<p>Pairly made {html.escape(str(job.entity_type))} "
            f"{html.escape(str(job.entity_id))}. The AI checks it now; a person approves it "
            f'in Pairly. <a href="{web}/en/inbox">Open the inbox</a></p><a href="/">Back</a>'
        )

    @app.post("/webhooks/pairly")
    async def webhook(request: Request) -> Response:
        body = await request.body()  # the raw bytes: verify before parsing
        try:
            event = construct_event(body, request.headers.get("Pairly-Signature"), secret)
        except WebhookSignatureError:
            return JSONResponse({"error": "bad signature"}, status_code=400)
        summary: dict[str, Any] = {"id": event.id, "type": event.type}
        if event.type == "approval.decided":
            decided = event.approval_decided()
            summary["approval"] = f"{web}/en/inbox/{decided['approval_id']}"
            summary["decision"] = decided["decision"]
        elif event.type == "batch.completed":
            summary["counts"] = event.batch_completed()["counts"]
        events.appendleft(summary)
        return Response(status_code=204)

    @app.get("/events")
    async def received() -> list[dict[str, Any]]:
        return list(events)

    return app


app = create_app()
