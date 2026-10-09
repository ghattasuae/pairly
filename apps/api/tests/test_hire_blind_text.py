# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind mode for messages and free text, on Postgres, through the API.

2. Approvals of sensitive messages while blind: the approver sees the exact message
   with the candidate's name, email and phone masked and labelled "hidden in blind
   mode"; the approval binds the hash of the real payload, and the email that is sent
   carries the real values. Once revealed, the approver sees the real message.
3. Free text while blind: notes, stage move reasons, message previews in the timeline
   and custom form answers go through the blind redactor with the candidate's known
   identity (name variants, email, phone, address) before reviewers see them; once
   revealed, they read as written.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.notifications.service import message_hash

from .hire_support import HireApi, HireWorld, cv_pdf, hire_api, open_job, owner_rows
from .test_hire_pipeline import _key, deliver, move, world

__all__ = ["hire_api", "world"]  # fixtures

NAME = "Layla Haddad"
PHONE = "+971 50 123 4567"
HIDDEN = "hidden in blind mode"


async def _apply(api: HireApi, world: HireWorld, job: dict[str, Any], **kw: Any) -> tuple[str, str]:
    email = f"layla.{uuid.uuid4().hex[:8]}@example.test"
    reference = await api.apply(
        world,
        job["slug"],
        cv_pdf(uuid.uuid4().hex),
        email=email,
        full_name=NAME,
        phone=PHONE,
        ip=f"198.51.100.{uuid.uuid4().int % 200 + 20}",
        **kw,
    )
    listed = await api.call("GET", f"/v1/hire/applications?job_id={job['id']}", world, "recruiter")
    (found,) = [a for a in listed.json()["items"] if a["reference"] == reference]
    return str(found["id"]), email


async def test_blind_approver_sees_the_message_masked_and_approves_the_real_one(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    app_id, email = await _apply(api, world, job)
    outbox = tmp_path / "outbox"
    await deliver(api, world, outbox)  # the acknowledgement

    moved = await move(api, world, app_id, "rejected")
    assert moved.status_code == 200, moved.text
    draft_id = moved.json()["draft_id"]
    note = f"If you have questions, call us back on {PHONE} or write from {email}, {NAME}."
    edited = await api.call(
        "PATCH",
        f"/v1/hire/messages/{draft_id}",
        world,
        "recruiter",
        json={"values": {"personal_note": note}},
    )
    assert edited.status_code == 200, edited.text
    submitted = await api.client.post(
        f"/v1/hire/messages/{draft_id}/submit",
        headers=api.headers(world, "recruiter", extra=_key()),
    )
    assert submitted.status_code == 200, submitted.text
    message = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert message["status"] == "awaiting_approval", message
    approval_id = message["approval_id"]

    # The approver's view: the exact message, with name, email and phone masked.
    detail = (await api.call("GET", f"/v1/approvals/{approval_id}", world, "manager")).json()
    assert detail["masked_fields"] == ["name", "email", "phone"]
    assert HIDDEN in detail["display_note"]
    shown = json.dumps(
        {"payload": detail["payload"], "context": detail["context"]}, ensure_ascii=False
    )
    for secret in (NAME, "Layla", "Haddad", email, PHONE, "50 123 4567"):
        assert secret not in shown, secret
    text = detail["payload"]["text"]
    assert "Dear [name hidden in blind mode]," in text
    assert "call us back on [phone hidden in blind mode]" in text
    assert "write from [email hidden in blind mode], [name hidden in blind mode]." in text
    assert detail["payload"]["recipient"] == "email:[email hidden in blind mode]"
    assert detail["context"]["text"] == text

    # The approval binds the hash of the real payload, which carries the real values.
    (stored,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload, payload_hash FROM approvals WHERE id = :a",
        {"a": approval_id},
    )
    assert stored.payload_hash == message_hash(stored.payload)
    assert detail["payload_hash"] == stored.payload_hash
    assert stored.payload["recipient"] == f"email:{email}"
    assert f"Dear {NAME}," in stored.payload["text"]
    assert PHONE in stored.payload["text"]
    assert email in stored.payload["text"]
    # Masked, the view is never what the hash covers.
    assert message_hash(detail["payload"]) != stored.payload_hash

    approved = await api.client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"decision": "approve"},
        headers=api.headers(world, "manager", extra=_key()),
    )
    assert approved.status_code == 200, approved.text
    sent = (await api.call("GET", f"/v1/hire/messages/{draft_id}", world, "recruiter")).json()
    assert sent["status"] == "queued", sent
    mails = await deliver(api, world, outbox)
    rejection = next(m for m in mails if "not to move forward" in m["text"])
    # The sent email carries the real values, exactly the approved payload.
    assert rejection["text"] == stored.payload["text"]
    assert rejection["to"] == [email]
    (notification,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT content_hash FROM notifications WHERE approval_id = :a",
        {"a": approval_id},
    )
    assert notification.content_hash == stored.payload_hash

    # Once revealed (a stage that reveals identity), the approver sees the real message.
    assert (await move(api, world, app_id, "shortlist")).status_code == 200
    after = (await api.call("GET", f"/v1/approvals/{approval_id}", world, "manager")).json()
    assert after["masked_fields"] == []
    assert after["display_note"] is None
    assert after["payload"] == stored.payload
