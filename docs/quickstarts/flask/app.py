# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Proofly Technology
"""Add human-approved AI to a Flask app in 10 minutes (Pairly quickstart).

    pip install pairly flask
    export PAIRLY_API_KEY=pk_test_...            # make sandbox prints one
    export PAIRLY_API_URL=http://127.0.0.1:8000  # the native stack
    export PAIRLY_WEB_URL=http://localhost:3100  # where people decide
    export PAIRLY_WEBHOOK_SECRET=whsec_...       # from Settings, Webhooks
    flask --app app run --port 5002

* ``GET  /``                  a form to submit an invoice, and the approvals waiting
* ``POST /submit``            sends the file to Pairly (``documents.submit``)
* ``POST /webhooks/pairly``   receives Pairly's signed webhooks
* ``GET  /events``            what the webhooks said

Pairly's AI reads and checks the invoice; a person approves it in Pairly (the link is on
the page). Your app never approves anything itself.
"""

from __future__ import annotations

import os
from collections import deque
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request

from pairly import Pairly
from pairly.webhooks import WebhookSignatureError, construct_event

HOME = """
<h1>Submit an invoice</h1>
<form method="post" action="/submit" enctype="multipart/form-data">
  <input type="file" name="file" required> <button>Submit</button>
</form>
<h2>Waiting for a person ({{ waiting|length }})</h2>
<ul>
{% for a in waiting %}
  <li>{{ a.title }} ({{ a.sla_state }})
      <a href="{{ web }}/en/inbox/{{ a.id }}">decide in Pairly</a></li>
{% endfor %}
</ul>
"""

SUBMITTED = """
<p>Submitted: job {{ job.job_id }}, {{ job.status }}.</p>
<p>Pairly made {{ job.entity_type }} {{ job.entity_id }}. The AI checks it now; a person
approves it in Pairly. <a href="{{ web }}/en/inbox">Open the inbox</a></p>
<a href="/">Back</a>
"""


def create_app(
    pairly: Pairly | None = None,
    *,
    webhook_secret: str | None = None,
    web_url: str | None = None,
) -> Flask:
    app = Flask(__name__)
    events: deque[dict[str, Any]] = deque(maxlen=50)
    secret = webhook_secret or os.environ.get("PAIRLY_WEBHOOK_SECRET", "")
    web = (web_url or os.environ.get("PAIRLY_WEB_URL") or "http://localhost:3100").rstrip("/")
    state: dict[str, Pairly] = {}

    def client() -> Pairly:
        # Created on first use, from PAIRLY_API_KEY and PAIRLY_API_URL.
        if "pairly" not in state:
            state["pairly"] = pairly or Pairly()
        return state["pairly"]

    @app.get("/")
    def home() -> str:
        return render_template_string(HOME, waiting=client().approvals.list_mine(), web=web)

    @app.post("/submit")
    def submit() -> str:
        upload = request.files["file"]
        job = client().documents.submit(
            upload.read(), workflow="pay", filename=upload.filename or "invoice.pdf"
        )
        return render_template_string(SUBMITTED, job=job, web=web)

    @app.post("/webhooks/pairly")
    def webhook() -> Response | tuple[Response, int]:
        body = request.get_data()  # the raw bytes: verify before parsing
        try:
            event = construct_event(body, request.headers.get("Pairly-Signature"), secret)
        except WebhookSignatureError:
            return jsonify(error="bad signature"), 400
        summary: dict[str, Any] = {"id": event.id, "type": event.type}
        if event.type == "approval.decided":
            decided = event.approval_decided()
            summary["approval"] = f"{web}/en/inbox/{decided['approval_id']}"
            summary["decision"] = decided["decision"]
        elif event.type == "batch.completed":
            summary["counts"] = event.batch_completed()["counts"]
        events.appendleft(summary)
        return Response(status=204)

    @app.get("/events")
    def received() -> Response:
        return jsonify(list(events))

    return app


app = create_app()
