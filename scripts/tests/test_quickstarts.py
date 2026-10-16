# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Smoke test of the Python quickstarts (docs/quickstarts/fastapi and flask).

Each app is imported from its file and driven end to end against a mock Pairly API:
the home page lists the approvals waiting with their "decide in Pairly" link, a file
submission goes to ``POST /v1/documents/batch``, and a webhook signed exactly as Pairly
signs it is accepted while a bad signature is refused. The TypeScript quickstarts have
their own smoke test (docs/quickstarts/smoke.test.ts).
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from pairly import AsyncPairly, Pairly
from pairly.webhooks import approval_decided_event, signed_delivery

ROOT = Path(__file__).resolve().parents[2] / "docs" / "quickstarts"
KEY = "pk_test_abcd1234_" + "A" * 43
SECRET = "whsec_" + "q" * 40
APPROVAL = str(uuid.uuid4())
WEB = "http://localhost:3100"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"quickstart_{name}", ROOT / name / "app.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def api(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/approvals":
        return httpx.Response(
            200,
            json={
                "items": [{"id": APPROVAL, "title": "Invoice INV-1", "sla_state": "on_track"}],
                "next_cursor": None,
            },
        )
    assert request.url.path == "/v1/documents/batch"
    assert request.url.params["workflow"] == "pay"
    assert b'filename="inv.pdf"' in request.content
    return httpx.Response(
        202,
        json={
            "id": "job-1",
            "status": "completed",
            "items": [
                {
                    "status": "accepted",
                    "document_id": "d1",
                    "entity_type": "pay.invoice",
                    "entity_id": "inv-1",
                }
            ],
        },
    )


def decided() -> tuple[bytes, dict[str, str]]:
    event = approval_decided_event(
        {
            "approval_id": APPROVAL,
            "run_id": "r",
            "workflow": "pay",
            "approval_type": "pay.invoice",
            "step": "manager",
            "decision_id": "d",
            "decision": "approve",
            "approval_status": "approved",
            "entity_type": "pay.invoice",
            "entity_id": "inv-1",
        },
        org_id=uuid.uuid4(),
    )
    return signed_delivery(event, SECRET)


def check(client: Any) -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert f'{WEB}/en/inbox/{APPROVAL}"' in home.text
    sent = client.post(
        "/submit", files={"file": ("inv.pdf", b"%PDF-1.7 synthetic", "application/pdf")}
    )
    assert sent.status_code == 200
    assert "job-1" in sent.text
    assert "pay.invoice inv-1" in sent.text
    body, headers = decided()
    assert client.post("/webhooks/pairly", content=body, headers=headers).status_code == 204
    signature = headers["Pairly-Signature"]
    flipped = signature[:-1] + ("1" if signature.endswith("0") else "0")
    bad = dict(headers, **{"Pairly-Signature": flipped})
    assert client.post("/webhooks/pairly", content=body, headers=bad).status_code == 400
    events = client.get("/events").json()
    assert events[0]["approval"] == f"{WEB}/en/inbox/{APPROVAL}"
    assert events[0]["decision"] == "approve"


def test_the_fastapi_quickstart() -> None:
    from fastapi.testclient import TestClient

    module = load("fastapi")
    pairly = AsyncPairly(api_key=KEY, transport=httpx.MockTransport(api))
    app = module.create_app(pairly, webhook_secret=SECRET, web_url=WEB)
    with TestClient(app) as client:
        check(client)


class _FlaskClient:
    """Flask's test client with the httpx-like calls ``check`` uses."""

    def __init__(self, app: Any) -> None:
        self._client = app.test_client()

    def get(self, path: str) -> Any:
        return _Wrapped(self._client.get(path))

    def post(self, path: str, **kwargs: Any) -> Any:
        if "files" in kwargs:
            name, content, media = kwargs.pop("files")["file"]
            import io

            kwargs["data"] = {"file": (io.BytesIO(content), name, media)}
            kwargs["content_type"] = "multipart/form-data"
        if "content" in kwargs:
            kwargs["data"] = kwargs.pop("content")
        return _Wrapped(self._client.post(path, **kwargs))


class _Wrapped:
    def __init__(self, response: Any) -> None:
        self.status_code = response.status_code
        self.text = response.get_data(as_text=True)
        self._response = response

    def json(self) -> Any:
        return self._response.get_json()


def test_the_flask_quickstart() -> None:
    module = load("flask")
    pairly = Pairly(api_key=KEY, transport=httpx.MockTransport(api))
    app = module.create_app(pairly, webhook_secret=SECRET, web_url=WEB)
    check(_FlaskClient(app))


@pytest.mark.parametrize("name", ["fastapi", "flask"])
def test_the_quickstarts_import_without_credentials(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PAIRLY_API_KEY", raising=False)
    assert load(name).app is not None
