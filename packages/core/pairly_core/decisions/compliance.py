# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Compliance pack export (brief 2.3.6).

``POST /v1/compliance/export`` stores a ``compliance_exports`` row (``pending``) and
queues ``compliance_export``. The worker builds one zip for the period and workflows:

* ``decision_records.jsonl``: every record decided in the period, as an ``internal``
  receipt (no decrypted personal data), with an integrity check per record.
* ``policy_versions.jsonl``: every policy version those records applied, plus every
  version published in the period (the YAML text included; policies hold no personal
  data).
* ``overrides.jsonl``: records of a person's action that superseded an AI record, with
  the AI output next to the human action.
* ``appeals.jsonl``: appeals filed or resolved in the period (reason and outcome text
  redacted: they are personal data).
* ``replays.jsonl``: replays run in the period.
* ``audit_events.json``: the audit export (format ``pairly.audit.v1``) for the period.
* ``manifest.json``: the period, the workflows, every other file with its SHA-256, size
  and entry count, the chain verification at export time, and what is not included yet.

The zip goes to object storage (``orgs/<org>/compliance/<export id>.zip``); the row
keeps its SHA-256 and the manifest's SHA-256, and both are audited
(``core.compliance.export_ready``). Downloads are short-lived signed URLs, audited.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from pairly_core.audit import (
    ActorType,
    AuditRecord,
    actor_ref,
    append_audit,
    canonical_json,
    format_timestamp,
    select_records,
)
from pairly_core.audit_anchor.verification import AnchorLoader, verify_with_anchors
from pairly_core.db import TenantError, current_org, set_tenant
from pairly_core.decisions.models import (
    HUMAN_KINDS,
    Appeal,
    ComplianceExport,
    DecisionRecord,
    DecisionReplay,
    ExportStatus,
)
from pairly_core.decisions.receipts import Audience, ReceiptContext, receipt
from pairly_core.decisions.service import open_sensitive, verify_record
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.ingestion.storage import ObjectStore
from pairly_core.models.audit import AuditEvent
from pairly_core.models.policy import PolicyVersion
from pairly_core.rbac import Actor

log = structlog.get_logger("pairly_core.decisions.compliance")

PACK_FORMAT: Final = "pairly.compliance_pack.v1"
AUDIT_EXPORT_FORMAT: Final = "pairly.audit.v1"
AUDIT_HASH_DEFINITION: Final = "sha256(bytes.fromhex(prev_hash) || canonical_event_json(event))"
EXPORT_REQUESTED: Final = "core.compliance.export_requested"
EXPORT_READY: Final = "core.compliance.export_ready"
EXPORT_FAILED: Final = "core.compliance.export_failed"
EXPORT_DOWNLOADED: Final = "core.compliance.export_downloaded"
SYSTEM_ACTOR_ID: Final = "pairly.worker.compliance"
MAX_RECORDS: Final = 200_000
MAX_ATTEMPTS: Final = 3
MAX_PERIOD_DAYS: Final = 3660
NOT_INCLUDED: Final = (
    {"item": "fairness_reports", "reason": "fairness and bias reports arrive with Hire (M2)"},
    {"item": "model_cards", "reason": "model cards are written per AI component (M6)"},
)


def compliance_key(org_id: uuid.UUID, export_id: uuid.UUID) -> str:
    return f"orgs/{org_id}/compliance/{export_id}.zip"


# ------------------------------------------------------------------ request


async def request_export(
    session: AsyncSession,
    actor: Actor,
    *,
    period_start: datetime,
    period_end: datetime,
    workflows: Sequence[str] = (),
) -> ComplianceExport:
    org = current_org(session)
    if org is None or org != actor.org_id:
        raise TenantError("exports need the actor's tenant context")
    if period_start.tzinfo is None or period_end.tzinfo is None:
        raise BadRequestError("the period needs timezone offsets")
    if period_end <= period_start:
        raise BadRequestError("the period must end after it starts")
    if (period_end - period_start).days > MAX_PERIOD_DAYS:
        raise BadRequestError("the period is too long")
    export_id = uuid.uuid4()
    row = ComplianceExport(
        id=export_id,
        org_id=org,
        requested_by=actor.id,
        period_start=period_start,
        period_end=period_end,
        workflows=sorted(set(workflows)),
        status=ExportStatus.PENDING.value,
    )
    session.add(row)
    await session.flush()
    kind, actor_id = actor_ref(actor)
    await append_audit(
        session,
        action=EXPORT_REQUESTED,
        actor_type=kind,
        actor_id=actor_id,
        entity_type="compliance_export",
        entity_id=export_id,
        payload={
            "from": format_timestamp(period_start),
            "to": format_timestamp(period_end),
            "workflows": sorted(set(workflows)),
        },
    )
    return row


async def get_export(session: AsyncSession, export_id: uuid.UUID) -> ComplianceExport | None:
    return await session.scalar(select(ComplianceExport).where(ComplianceExport.id == export_id))


async def download_url(
    session: AsyncSession,
    actor: Actor,
    store: ObjectStore,
    export_id: uuid.UUID,
    *,
    expires_in: int,
) -> str:
    row = await get_export(session, export_id)
    if row is None:
        raise NotFoundError("export not found")
    if row.status != ExportStatus.READY.value or row.storage_key is None:
        raise ConflictError("the export is not ready")
    kind, actor_id = actor_ref(actor)
    await append_audit(
        session,
        action=EXPORT_DOWNLOADED,
        actor_type=kind,
        actor_id=actor_id,
        entity_type="compliance_export",
        entity_id=row.id,
        payload={"sha256": row.sha256, "expires_in": expires_in},
    )
    stamp = row.created_at.astimezone(UTC).strftime("%Y%m%d")
    return store.presigned_get_url(
        row.storage_key,
        expires_in=expires_in,
        filename=f"compliance-pack-{stamp}-{row.id}.zip",
        content_type="application/zip",
    )


# ------------------------------------------------------------------ build


@dataclass
class _File:
    name: str
    path: Path
    entries: int = 0

    def sha256(self) -> str:
        digest = hashlib.sha256()
        with self.path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()


def _line(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _in_scope(row: ComplianceExport) -> list[Any]:
    clauses: list[Any] = [
        DecisionRecord.decided_at >= row.period_start,
        DecisionRecord.decided_at < row.period_end,
    ]
    if row.workflows:
        clauses.append(DecisionRecord.workflow.in_(list(row.workflows)))
    return clauses


async def _write_records(
    session: AsyncSession, row: ComplianceExport, out: _File
) -> tuple[set[uuid.UUID], set[str]]:
    policy_ids: set[uuid.UUID] = set()
    record_ids: set[str] = set()
    query = (
        select(DecisionRecord)
        .where(*_in_scope(row))
        .order_by(DecisionRecord.decided_at, DecisionRecord.id)
        .limit(MAX_RECORDS + 1)
    )
    with out.path.open("wb") as handle:
        for record in (await session.scalars(query)).all():
            if out.entries >= MAX_RECORDS:
                raise ValueError("too_many_records")
            problem = await verify_record(session, record)
            sensitive = None if problem == "decryption_failed" else open_sensitive(record)
            data = receipt(
                record,
                ReceiptContext(sensitive=sensitive, intact=problem is None),
                Audience.INTERNAL,
            )
            data["integrity"] = {"content_hash": record.content_hash, "problem": problem}
            handle.write(_line(data))
            out.entries += 1
            record_ids.add(record.id)
            for ref in record.policy_versions:
                try:
                    policy_ids.add(uuid.UUID(str(ref.get("id"))))
                except ValueError:
                    continue
    return policy_ids, record_ids


async def _write_policies(
    session: AsyncSession, row: ComplianceExport, policy_ids: Iterable[uuid.UUID], out: _File
) -> None:
    query = (
        select(PolicyVersion)
        .where(
            or_(
                PolicyVersion.id.in_(list(policy_ids)),
                and_(
                    PolicyVersion.created_at >= row.period_start,
                    PolicyVersion.created_at < row.period_end,
                ),
            )
        )
        .order_by(PolicyVersion.kind, PolicyVersion.name, PolicyVersion.version)
    )
    with out.path.open("wb") as handle:
        for policy in (await session.scalars(query)).all():
            handle.write(
                _line(
                    {
                        "id": str(policy.id),
                        "kind": policy.kind,
                        "name": policy.name,
                        "version": policy.version,
                        "content_hash": policy.content_hash,
                        "created_at": format_timestamp(policy.created_at),
                        "created_by_type": policy.created_by_type,
                        "created_by_id": policy.created_by_id,
                        "content": policy.content,
                    }
                )
            )
            out.entries += 1


async def _write_overrides(session: AsyncSession, row: ComplianceExport, out: _File) -> None:
    earlier = aliased(DecisionRecord)
    human = [k.value for k in HUMAN_KINDS]
    query = (
        select(DecisionRecord, earlier)
        .join(
            earlier,
            and_(earlier.org_id == DecisionRecord.org_id, earlier.id == DecisionRecord.supersedes),
        )
        .where(*_in_scope(row), DecisionRecord.kind.in_(human), earlier.kind.not_in(human))
        .order_by(DecisionRecord.decided_at, DecisionRecord.id)
        .limit(MAX_RECORDS)
    )
    with out.path.open("wb") as handle:
        for human_row, ai_row in (await session.execute(query)).all():
            handle.write(
                _line(
                    {
                        "decision_id": human_row.id,
                        "kind": human_row.kind,
                        "workflow": human_row.workflow,
                        "subject": {"type": human_row.entity_type, "id": human_row.entity_id},
                        "human_action": human_row.final_action,
                        "actor": {"type": human_row.actor_type, "id": human_row.actor_id},
                        "decided_at": format_timestamp(human_row.decided_at),
                        "superseded_decision_id": ai_row.id,
                        "superseded_kind": ai_row.kind,
                        "ai_output": ai_row.output,
                        "ai_confidence": ai_row.confidence,
                    }
                )
            )
            out.entries += 1


async def _write_appeals(
    session: AsyncSession, row: ComplianceExport, record_ids: set[str], out: _File
) -> None:
    query = (
        select(Appeal)
        .where(
            or_(
                and_(Appeal.created_at >= row.period_start, Appeal.created_at < row.period_end),
                and_(Appeal.decided_at >= row.period_start, Appeal.decided_at < row.period_end),
            )
        )
        .order_by(Appeal.created_at, Appeal.id)
        .limit(MAX_RECORDS)
    )
    with out.path.open("wb") as handle:
        for appeal in (await session.scalars(query)).all():
            if row.workflows and appeal.decision_id not in record_ids:
                workflow = await session.scalar(
                    select(DecisionRecord.workflow).where(DecisionRecord.id == appeal.decision_id)
                )
                if workflow not in row.workflows:
                    continue
            handle.write(
                _line(
                    {
                        "id": str(appeal.id),
                        "decision_id": appeal.decision_id,
                        "requester_type": appeal.requester_type,
                        "status": appeal.status,
                        "outcome": appeal.outcome,
                        "reason": {"redacted": True},
                        "outcome_text": {"redacted": True} if appeal.outcome_text else None,
                        "created_at": format_timestamp(appeal.created_at),
                        "sla_due_at": format_timestamp(appeal.sla_due_at),
                        "escalated_at": (
                            format_timestamp(appeal.escalated_at) if appeal.escalated_at else None
                        ),
                        "decided_at": (
                            format_timestamp(appeal.decided_at) if appeal.decided_at else None
                        ),
                        "decided_by": str(appeal.decided_by) if appeal.decided_by else None,
                        "within_sla": (
                            appeal.decided_at <= appeal.sla_due_at if appeal.decided_at else None
                        ),
                        "outcome_decision_id": appeal.outcome_decision_id,
                    }
                )
            )
            out.entries += 1


async def _write_replays(session: AsyncSession, row: ComplianceExport, out: _File) -> None:
    query = (
        select(DecisionReplay, DecisionRecord.workflow)
        .join(
            DecisionRecord,
            and_(
                DecisionRecord.org_id == DecisionReplay.org_id,
                DecisionRecord.id == DecisionReplay.decision_id,
            ),
        )
        .where(
            DecisionReplay.created_at >= row.period_start,
            DecisionReplay.created_at < row.period_end,
        )
        .order_by(DecisionReplay.created_at, DecisionReplay.id)
        .limit(MAX_RECORDS)
    )
    if row.workflows:
        query = query.where(DecisionRecord.workflow.in_(list(row.workflows)))
    with out.path.open("wb") as handle:
        for replay, _workflow in (await session.execute(query)).all():
            handle.write(
                _line(
                    {
                        "id": str(replay.id),
                        "decision_id": replay.decision_id,
                        "mode": replay.mode,
                        "deterministic": replay.deterministic,
                        "status": replay.status,
                        "reason": replay.reason,
                        "diff": replay.diff,
                        "requested_by": {
                            "type": replay.requested_by_type,
                            "id": replay.requested_by_id,
                        },
                        "created_at": format_timestamp(replay.created_at),
                    }
                )
            )
            out.entries += 1


async def _write_audit(session: AsyncSession, row: ComplianceExport, out: _File) -> None:
    header = {
        "format": AUDIT_EXPORT_FORMAT,
        "org_id": str(row.org_id),
        "exported_at": format_timestamp(datetime.now(UTC)),
        "hash": AUDIT_HASH_DEFINITION,
        "filters": {
            "from": format_timestamp(row.period_start),
            "to": format_timestamp(row.period_end),
        },
    }
    head = json.dumps(header, separators=(",", ":"))
    query = (
        select_records()
        .where(AuditEvent.occurred_at >= row.period_start, AuditEvent.occurred_at < row.period_end)
        .order_by(AuditEvent.seq)
        .execution_options(yield_per=500)
    )
    with out.path.open("wb") as handle:
        handle.write((head[:-1] + ',"events":[').encode("utf-8"))
        result = await session.stream(query)
        async for event in result:
            prefix = b"," if out.entries else b""
            handle.write(prefix + canonical_json(AuditRecord.from_row(event).to_json()))
            out.entries += 1
        handle.write(b"]}")


async def build_pack(
    session: AsyncSession,
    row: ComplianceExport,
    workdir: Path,
    *,
    anchors: AnchorLoader | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write the pack's files and zip into ``workdir``. Returns the zip and manifest."""
    files = {
        name: _File(name, workdir / name)
        for name in (
            "decision_records.jsonl",
            "policy_versions.jsonl",
            "overrides.jsonl",
            "appeals.jsonl",
            "replays.jsonl",
            "audit_events.json",
        )
    }
    policy_ids, record_ids = await _write_records(session, row, files["decision_records.jsonl"])
    await _write_policies(session, row, policy_ids, files["policy_versions.jsonl"])
    await _write_overrides(session, row, files["overrides.jsonl"])
    await _write_appeals(session, row, record_ids, files["appeals.jsonl"])
    await _write_replays(session, row, files["replays.jsonl"])
    await _write_audit(session, row, files["audit_events.json"])
    chain, info = await verify_with_anchors(session, row.org_id, anchors)
    manifest: dict[str, Any] = {
        "format": PACK_FORMAT,
        "export_id": str(row.id),
        "org_id": str(row.org_id),
        "period": {
            "from": format_timestamp(row.period_start),
            "to": format_timestamp(row.period_end),
        },
        "workflows": list(row.workflows),
        "generated_at": format_timestamp(datetime.now(UTC)),
        "redaction": "internal: decrypted personal data is not included",
        "files": [
            {
                "name": f.name,
                "sha256": f.sha256(),
                "bytes": f.path.stat().st_size,
                "entries": f.entries,
            }
            for f in files.values()
        ],
        "audit_chain": chain.to_json() | info,
        "not_included": list(NOT_INCLUDED),
    }
    manifest_path = workdir / "manifest.json"
    manifest_path.write_bytes(canonical_json(manifest))
    zip_path = workdir / "pack.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for f in files.values():
            archive.write(f.path, arcname=f.name)
        archive.write(manifest_path, arcname="manifest.json")
    return zip_path, manifest


def _sha256_file(path: Path) -> str:
    return _File("x", path).sha256()


async def run_export(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore | None,
    org_id: uuid.UUID,
    export_id: uuid.UUID,
    *,
    anchors: AnchorLoader | None = None,
) -> dict[str, Any]:
    """Claim one export, build it, store the zip and mark it ready (or failed)."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            claimed = await session.execute(
                update(ComplianceExport)
                .where(
                    ComplianceExport.id == export_id,
                    ComplianceExport.status.in_(
                        [ExportStatus.PENDING.value, ExportStatus.RUNNING.value]
                    ),
                    ComplianceExport.attempts < MAX_ATTEMPTS,
                )
                .values(
                    status=ExportStatus.RUNNING.value,
                    attempts=ComplianceExport.attempts + 1,
                    started_at=datetime.now(UTC),
                )
                .returning(ComplianceExport.id)
            )
            if claimed.first() is None:
                return {"status": "skipped"}
        error: str | None = None
        manifest: dict[str, Any] = {}
        digest = manifest_digest = ""
        size = 0
        key = compliance_key(org_id, export_id)
        try:
            if store is None:
                raise RuntimeError("storage_unavailable")
            with tempfile.TemporaryDirectory(prefix="pairly-pack-") as tmp:
                async with session.begin():
                    row = await get_export(session, export_id)
                    if row is None:  # pragma: no cover - claimed above
                        raise RuntimeError("export_missing")
                    zip_path, manifest = await build_pack(session, row, Path(tmp), anchors=anchors)
                digest = _sha256_file(zip_path)
                manifest_digest = hashlib.sha256(canonical_json(manifest)).hexdigest()
                size = zip_path.stat().st_size
                await store.put_file(key, zip_path, content_type="application/zip", sha256=digest)
        except Exception as exc:
            error = (
                str(exc)
                if str(exc) in {"storage_unavailable", "too_many_records"}
                else (f"export_error:{type(exc).__name__}")
            )
            log.error("compliance_export_failed", org_id=str(org_id), error=error)
        async with session.begin():
            if error is None:
                await session.execute(
                    update(ComplianceExport)
                    .where(ComplianceExport.id == export_id)
                    .values(
                        status=ExportStatus.READY.value,
                        storage_key=key,
                        size_bytes=size,
                        sha256=digest,
                        manifest_sha256=manifest_digest,
                        manifest=manifest,
                        completed_at=datetime.now(UTC),
                        error=None,
                    )
                )
            else:
                await session.execute(
                    update(ComplianceExport)
                    .where(ComplianceExport.id == export_id)
                    .values(
                        status=ExportStatus.FAILED.value,
                        error=error[:128],
                        completed_at=datetime.now(UTC),
                    )
                )
            await append_audit(
                session,
                action=EXPORT_READY if error is None else EXPORT_FAILED,
                actor_type=ActorType.SYSTEM,
                actor_id=SYSTEM_ACTOR_ID,
                entity_type="compliance_export",
                entity_id=export_id,
                payload=(
                    {"sha256": digest, "manifest_sha256": manifest_digest, "bytes": size}
                    if error is None
                    else {"error": error[:128]}
                ),
            )
    return {"status": "ready" if error is None else "failed", "error": error}


async def stale_exports(session: AsyncSession) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(ComplianceExport.id)
        .where(
            ComplianceExport.status.in_([ExportStatus.PENDING.value, ExportStatus.RUNNING.value]),
            ComplianceExport.attempts < MAX_ATTEMPTS,
            func.coalesce(ComplianceExport.started_at, ComplianceExport.created_at)
            < func.now() - timedelta(minutes=2),
        )
        .order_by(ComplianceExport.created_at)
        .limit(5)
    )
    return list(rows)


async def give_up_exports(session: AsyncSession) -> int:
    result = await session.execute(
        update(ComplianceExport)
        .where(
            ComplianceExport.status.in_([ExportStatus.PENDING.value, ExportStatus.RUNNING.value]),
            ComplianceExport.attempts >= MAX_ATTEMPTS,
        )
        .values(
            status=ExportStatus.FAILED.value,
            error="attempts_exhausted",
            completed_at=datetime.now(UTC),
        )
        .returning(ComplianceExport.id)
    )
    return len(result.all())


def manifest_files(manifest: Mapping[str, Any]) -> dict[str, str]:
    """``name -> sha256`` from a manifest (for checking a downloaded pack)."""
    return {str(f["name"]): str(f["sha256"]) for f in manifest.get("files", [])}
