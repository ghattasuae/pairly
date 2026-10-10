# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Complete erasure of a candidate (``pairly_hire.erasure``) on Postgres, through the API.

A candidate is screened end to end (CV upload, parse, extraction, model calls, decision
records, approval, workflow checkpoints, notifications), shortlisted, and then gathers a
note with a mention, a stage move reason, a portal reply, an interview with its panel
briefing, a review request, self-identification answers, portal links and a deletion
request. An admin erases them. Afterwards:

* a search of every table (plain columns, decrypted encrypted columns, checkpoint blobs)
  and of every stored object for the candidate's name, email, phone and a sentence of
  their CV finds nothing (and found each of them before);
* the candidate's stored files are gone; the other candidate's are not;
* the audit chain verifies, and the candidate's decision records verify as ``erased``;
* the candidate's data export is an empty, erased marker.

A store that fails leaves the request open with the error for the admin; the scheduled
job finishes it once the store works.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.audit import AuditRecord, verify_records
from pairly_core.crypto.envelope import Aad
from pairly_core.crypto.types import Sealed, encrypted_columns
from pairly_core.crypto.types import seal as _seal
from pairly_core.db import Base, create_sessionmaker, set_tenant
from pairly_core.decisions.models import Appeal, DecisionRecord
from pairly_core.decisions.service import verify_record
from pairly_core.documents.model import NormalizedDocument
from pairly_core.ingestion.storage import InMemoryObjectStore
from pairly_core.jobs.registry import JobContext
from pairly_core.notifications.models import Notification
from pairly_hire.erasure import run_erasure_job
from pairly_hire.messaging.replies import record_reply
from pairly_hire.models import HireApplication, HireCandidate
from pairly_hire.pipeline.models import HireStageMove
from pairly_hire.portal.auth import PortalIdentity
from pairly_hire.portal.models import HireDataRequest, HirePortalSession, HirePortalToken
from pairly_hire.portal.service import export_data
from pairly_hire.scheduling.models import HireInterview
from pairly_hire.screening.models import HireScreening
from pairly_hire.selfid.models import HireSelfIdentification
from synth.hire.screening import contact_for

from .hire_support import HireWorld, owner_rows
from .test_hire_screening import Screen, ScreenFactory, screen_factory, spec, world

__all__ = ["screen_factory", "world"]  # fixtures

CANDIDATE = "cv-0026"


def seal(model: Any, column: str, value: Any, *, org_id: uuid.UUID, row_id: object) -> Any:
    """A value for an encrypted column (typed ``Any``: the column expects ``Sealed``)."""
    return _seal(model, column, value, org_id=org_id, row_id=row_id)


OTHER = "cv-0029"


async def _sentence(store: InMemoryObjectStore, needles: list[str]) -> str:
    """A long line of the candidate's parsed CV that names no other needle."""
    for key, obj in store.objects.items():
        if "/derived/" not in key:
            continue
        doc = NormalizedDocument.model_validate_json(obj.data)
        if needles[1] not in doc.text and needles[0] not in doc.text:
            continue
        for line in doc.text.splitlines():
            line = line.strip()
            if len(line) >= 40 and not any(n in line for n in needles):
                return line[:60]
    raise AssertionError("no parsed CV in the store")


def _texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bytes | memoryview):
        raw = bytes(value)
        return [raw.decode("utf-8", "ignore")]
    if isinstance(value, dict | list):
        return [json.dumps(value, ensure_ascii=False, default=str)]
    return [str(value)]


async def _haystack(
    owner: AsyncEngine, org_id: uuid.UUID, store: InMemoryObjectStore
) -> list[tuple[str, str]]:
    """``(where, text)`` for every value the organisation holds."""
    out: list[tuple[str, str]] = []
    async with owner.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        tables = (
            await conn.execute(
                text(
                    "SELECT t.table_schema, t.table_name, "
                    "EXISTS (SELECT 1 FROM information_schema.columns c "
                    "WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name "
                    "AND c.column_name = 'org_id') AS scoped "
                    "FROM information_schema.tables t "
                    "WHERE t.table_schema IN ('public', 'lg_checkpoints') "
                    "AND t.table_type = 'BASE TABLE'"
                )
            )
        ).all()
        for schema, table, scoped in tables:
            where = " WHERE org_id = :o" if scoped else ""
            rows = await conn.execute(
                text(f'SELECT * FROM "{schema}"."{table}"{where}'),  # noqa: S608  # nosec B608
                {"o": org_id},
            )
            for row in rows.mappings():
                for column, value in row.items():
                    out += [(f"{schema}.{table}.{column}", t) for t in _texts(value)]
        for enc in encrypted_columns(Base.metadata):
            table = enc.table
            col = table.c[enc.column]
            rows = await conn.execute(
                select(table.c.id, col).where(table.c.org_id == org_id, col.is_not(None))
            )
            for row_id, sealed in rows.all():
                assert isinstance(sealed, Sealed)
                aad = Aad.for_row(org_id, table.name, enc.column, row_id)
                decoder: Any = col.type
                value = decoder.decode(sealed.open_bytes(aad))
                out += [(f"{table.name}.{enc.column} (decrypted)", t) for t in _texts(value)]
    for key, obj in store.objects.items():
        if f"orgs/{org_id}/" in key:
            out.append((f"object {key}", obj.data.decode("utf-8", "ignore")))
    return out


def _found(haystack: list[tuple[str, str]], needle: str) -> list[str]:
    return sorted({where for where, value in haystack if needle in value})


async def _shortlisted(api: Screen, world: HireWorld, job: dict[str, Any], doc: str) -> str:
    app_id = await api.apply(world, job, doc)
    view = await api.screening(world, app_id)
    assert view["status"] == "awaiting_review", view["review_reasons"]
    decided = await api.decide(world, view["approval_id"], "approve")
    assert decided.status_code == 200, decided.text
    return app_id


async def _gather(
    api: Screen, world: HireWorld, owner: AsyncEngine, app_id: str, name: str, email: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Everything else a candidate collects. Returns the candidate and request ids."""
    recruiter = world.people["recruiter"][0]
    manager = world.people["manager"][0]
    noted = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/notes",
        world,
        "recruiter",
        json={"body": f"{name} answered well, call {email}.", "mentions": [str(manager)]},
    )
    assert noted.status_code == 201, noted.text
    org = world.org_id
    session = create_sessionmaker(owner)()
    async with session:
        set_tenant(session, org, None)
        async with session.begin():
            app = await session.scalar(
                select(HireApplication).where(HireApplication.id == uuid.UUID(app_id))
            )
            assert app is not None
            cid = app.candidate_id
            now = datetime.now(UTC)
            await record_reply(session, app.id, text=f"Thanks, {name} here.", subject=name)
            move = HireStageMove(
                id=uuid.uuid4(),
                org_id=org,
                application_id=app.id,
                job_id=app.job_id,
                from_stage="screening",
                to_stage="interview",
                actor_type="user",
                actor_id=str(recruiter),
            )
            move.reason = seal(HireStageMove, "reason", f"{name} fits", org_id=org, row_id=move.id)
            interview = HireInterview(
                id=uuid.uuid4(),
                org_id=org,
                application_id=app.id,
                job_id=app.job_id,
                candidate_id=cid,
                stage_key="interview",
                status="booked",
                duration_min=30,
                starts_at=now + timedelta(days=2),
                ends_at=now + timedelta(days=2, minutes=30),
                candidate_timezone="Asia/Dubai",
                candidate_locale="en",
                organizer_id=recruiter,
                ics_uid=f"iv-{uuid.uuid4().hex}@pairly.test",
                link_nonce="0123456789abcdef",
                created_by_type="user",
                created_by_id=str(recruiter),
            )
            interview.meeting_url = seal(
                HireInterview,
                "meeting_url",
                f"https://meet.example.test/{email}",
                org_id=org,
                row_id=interview.id,
            )
            briefing = Notification(
                id=uuid.uuid4(),
                org_id=org,
                recipient_user_id=manager,
                channel="email",
                category="updates",
                urgency="normal",
                template_key="hire.interview.briefing",
                locale="en",
                status="sent",
                content_hash="0" * 64,
                dedupe_key=f"hire.iv:{interview.id}:0:x:brief:{manager}",
                workflow="hire",
            )
            briefing.content = seal(
                Notification,
                "content",
                {"text": f"Interview {name}", "attendee": email},
                org_id=org,
                row_id=briefing.id,
            )
            request = HireDataRequest(
                id=uuid.uuid4(), org_id=org, candidate_id=cid, kind="deletion", status="open"
            )
            request.note = seal(
                HireDataRequest, "note", f"Please delete {name}", org_id=org, row_id=request.id
            )
            selfid = HireSelfIdentification(
                id=uuid.uuid4(), org_id=org, application_id=app.id, job_id=app.job_id
            )
            selfid.answers = seal(
                HireSelfIdentification, "answers", {"note": name}, org_id=org, row_id=selfid.id
            )
            later = now + timedelta(days=1)
            session.add_all(
                [
                    move,
                    interview,
                    briefing,
                    request,
                    selfid,
                    HirePortalToken(
                        id=uuid.uuid4(),
                        org_id=org,
                        candidate_id=cid,
                        secret_hash="a" * 64,
                        purpose="request",
                        locale="en",
                        expires_at=later,
                    ),
                    HirePortalSession(
                        id=uuid.uuid4(),
                        org_id=org,
                        candidate_id=cid,
                        secret_hash="b" * 64,
                        expires_at=later,
                    ),
                ]
            )
            score_id = await session.scalar(
                select(HireScreening.score_decision_id).where(
                    HireScreening.application_id == app.id
                )
            )
            assert score_id is not None
            appeal = Appeal(
                id=uuid.uuid4(),
                org_id=org,
                decision_id=score_id,
                requester_type="candidate",
                requester_id=f"hire_candidate:{cid}",
                status="open",
                sla_due_at=later,
            )
            appeal.reason = seal(
                Appeal, "reason", f"I am {name}, please look again", org_id=org, row_id=appeal.id
            )
            session.add(appeal)
    return cid, request.id


async def test_erasure_leaves_nothing_but_ids_hashes_and_outcomes(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    store = api.services.store
    assert isinstance(store, InMemoryObjectStore)
    job = await api.open_job(world)
    app_id = await _shortlisted(api, world, job, CANDIDATE)
    other_id = await _shortlisted(api, world, job, OTHER)
    contact = contact_for(spec(CANDIDATE))
    name, email, phone = contact["full_name"], contact["email"], contact["phone"]
    cid, request_id = await _gather(api, world, owner_engine, app_id, name, email)
    sentence = await _sentence(store, [name, email, phone])
    needles = {"name": name, "email": email, "phone": phone, "cv sentence": sentence}

    before = await _haystack(owner_engine, world.org_id, store)
    for label, needle in needles.items():
        assert _found(before, needle), f"the {label} is not stored anywhere to begin with"
    docs = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT d.id, d.sha256, d.storage_key, d.parse_result_key FROM documents d "
        "JOIN hire_applications a ON a.cv_document_id = d.id WHERE a.id = :a",
        {"a": app_id},
    )
    (doc,) = docs
    (other_doc,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT d.storage_key FROM documents d "
        "JOIN hire_applications a ON a.cv_document_id = d.id WHERE a.id = :a",
        {"a": other_id},
    )
    assert doc.storage_key in store.objects
    assert doc.parse_result_key in store.objects

    erased = await api.call(
        "POST",
        f"/v1/hire/data-requests/{request_id}/erase",
        world,
        "admin",
        json={"reason": "The candidate asked us to delete everything."},
    )
    assert erased.status_code == 200, erased.text
    assert erased.json()["status"] == "completed"
    assert erased.json()["erasure_pending"] is False

    after = await _haystack(owner_engine, world.org_id, store)
    for label, needle in needles.items():
        assert not _found(after, needle), f"the {label} is still in {_found(after, needle)}"
    assert doc.storage_key not in store.objects
    assert doc.parse_result_key not in store.objects
    assert other_doc.storage_key in store.objects
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT status, sha256, size_bytes, erased_at, objects_erased_at, parse_result_key "
        "FROM documents WHERE id = :d",
        {"d": doc.id},
    )
    assert row.status == "erased"
    assert row.sha256 == doc.sha256
    assert row.size_bytes > 0
    assert row.erased_at is not None
    assert row.objects_erased_at is not None
    assert row.parse_result_key is None
    # Gone, not just cleared: portal links, sessions and self-identification answers.
    for table in ("hire_portal_tokens", "hire_portal_sessions"):
        assert not await owner_rows(
            owner_engine,
            world.org_id,
            f"SELECT 1 FROM {table} WHERE candidate_id = :c",  # noqa: S608  # nosec B608
            {"c": cid},
        )
    assert not await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT 1 FROM hire_self_identification WHERE application_id = :a",
        {"a": app_id},
    )
    # Outcomes stay: the application, its stage history and its scores.
    (app_row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT a.status, s.overall_score, s.decision_ids FROM hire_applications a "
        "JOIN hire_screenings s ON s.application_id = a.id WHERE a.id = :a",
        {"a": app_id},
    )
    assert app_row.overall_score is not None
    assert app_row.decision_ids

    # The audit chain still verifies, and holds the erasure.
    exported = await api.call("GET", "/v1/audit/export?format=json", world, "auditor")
    assert exported.status_code == 200, exported.text
    records = [AuditRecord.from_json(e) for e in exported.json()["events"]]
    assert verify_records(world.org_id, records).ok
    actions = [r.action for r in records]
    for action in (
        "hire.data_request.erasure_started",
        "core.decision.sensitive_erased",
        "hire.data_request.erased",
    ):
        assert action in actions

    # Decision records: the candidate's verify as erased, the other candidate's as intact.
    session = create_sessionmaker(api.engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            rows = list(await session.scalars(select(DecisionRecord)))
            erased_ids = [r.id for r in rows if r.sensitive_erased_at is not None]
            assert erased_ids
            results = {r.id: await verify_record(session, r) for r in rows}
            assert {results[i] for i in erased_ids} == {"erased"}
            assert {v for k, v in results.items() if k not in erased_ids} == {None}
            candidate = await session.scalar(select(HireCandidate).where(HireCandidate.id == cid))
            assert candidate is not None
            assert candidate.erased_at is not None
            export = await export_data(session, PortalIdentity(world.org_id, cid, uuid.uuid4()))
    assert export["erased"] is True
    assert export["candidate"] is None
    assert export["applications"] == []
    receipt = await api.call("GET", f"/v1/decisions/{erased_ids[0]}/receipt", world, "admin")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["integrity"]["erased"] is True
    # The application role can erase a record's personal part, nothing else: no
    # re-wrap, no second erasure, no other column.
    intact = next(k for k, v in results.items() if v is None)
    for sql, record_id in (
        ("UPDATE decision_records SET sensitive = sensitive WHERE id = :i", intact),
        ("UPDATE decision_records SET sensitive_erased_at = now() WHERE id = :i", erased_ids[0]),
        ("UPDATE decision_records SET output = '{}'::jsonb WHERE id = :i", intact),
        ("UPDATE model_calls SET response = NULL", None),
    ):
        session = create_sessionmaker(api.engine)()
        async with session:
            set_tenant(session, world.org_id, None)
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(sql), {"i": record_id})


class _BrokenStore(InMemoryObjectStore):
    async def delete(self, key: str) -> None:
        from pairly_core.ingestion.storage import StorageError

        raise StorageError("store down")


async def test_a_failing_store_keeps_the_request_open_until_the_job_finishes(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    store = api.services.store
    assert isinstance(store, InMemoryObjectStore)
    job = await api.open_job(world)
    app_id = await api.apply(world, job, CANDIDATE)
    contact = contact_for(spec(CANDIDATE))
    _, request_id = await _gather(
        api, world, owner_engine, app_id, contact["full_name"], contact["email"]
    )
    broken = _BrokenStore(objects=store.objects)
    runtime = api.app.state.workflow_runtime
    original = runtime.services
    runtime.services = dataclasses.replace(original, store=broken)
    try:
        erased = await api.call(
            "POST",
            f"/v1/hire/data-requests/{request_id}/erase",
            world,
            "admin",
            json={"reason": "The candidate asked us to delete everything."},
        )
        assert erased.status_code == 200, erased.text
        body = erased.json()
        # Fail closed: the rows are erased, the request stays open with the error.
        assert body["status"] == "open"
        assert body["erasure_pending"] is True
        assert body["erasure_error"] == "object_store_delete_failed"
        assert body["candidate_name"] == "Erased candidate"
        resolve = await api.call(
            "POST",
            f"/v1/hire/data-requests/{request_id}/resolve",
            world,
            "admin",
            json={"status": "completed", "outcome": "Done by hand"},
        )
        assert resolve.status_code == 409
        context = JobContext(runtime.sessionmaker, runtime, runtime.registry)
        assert (await run_erasure_job(context))["failed"] >= 1
    finally:
        runtime.services = original
    totals = await run_erasure_job(JobContext(runtime.sessionmaker, runtime, runtime.registry))
    assert totals["completed"] >= 1
    done = await api.call("GET", f"/v1/hire/data-requests/{request_id}", world, "admin")
    assert done.json()["status"] == "completed"
    assert done.json()["erasure_error"] is None
    failed = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT payload FROM audit_events WHERE action = 'hire.data_request.erasure_failed'",
    )
    assert len(failed) >= 2
