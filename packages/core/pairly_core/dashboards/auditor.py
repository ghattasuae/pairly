# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The auditor dashboard (brief 5.1 item 12), read only.

* **Action needed**: a broken audit chain (the integrity check of this request, or a
  failed nightly verification recorded in the log), and an anchor that does not verify.
* **Audit log**: events in all, in the last 7 days, the denials of the last 7 days and
  the latest events, with the search page and the export as entry points.
* **Integrity**: the chain checked now (``verify_with_anchors``, as ``GET
  /v1/audit/verify``) when it is short enough to check inline, else the latest stored
  verification; and the anchored heads (``GET /v1/audit/anchors``).
* **Approvals history**: decisions of the last 30 days by outcome, the latest ones.
* **Overrides**: human records that superseded an AI record, with whether a reason was
  recorded. The reason itself is personal data (encrypted, redacted in the compliance
  pack): it is shown on the decision receipt, where viewing it is audited.
* **AI decisions**: the latest AI records with their model and prompt versions.
* Workflow plugins add their own (Pairly Pay: bank-change tickets and their
  verification evidence).

The viewer only sees the workflows their permissions cover (approvals and decision
records follow ``pairly_core.decisions.scope``). Every count links to its records.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from pairly_core.approvals.models import Approval, ApprovalDecision
from pairly_core.audit import AuditAction
from pairly_core.audit_anchor.anchoring import anchor_map, load_anchors, public_keys
from pairly_core.audit_anchor.models import AuditVerification
from pairly_core.audit_anchor.signing import AnchorError
from pairly_core.audit_anchor.store import AnchorStore, AnchorStoreError
from pairly_core.audit_anchor.verification import chain_length, verify_with_anchors
from pairly_core.dashboards.model import (
    Board,
    Dashboard,
    Item,
    Link,
    Section,
    SectionContext,
    SectionRegistry,
    Stat,
    plugin_sections,
    section,
)
from pairly_core.decisions.models import HUMAN_KINDS, DecisionRecord
from pairly_core.decisions.scope import read_scope
from pairly_core.models import AuditEvent
from pairly_core.rbac import Actor

HISTORY_DAYS: Final = 30
RECENT_DAYS: Final = 7
LIST: Final = 10


async def _audit_log(session: AsyncSession, now: datetime) -> Section:
    week = now - timedelta(days=RECENT_DAYS)
    total = int(await session.scalar(select(func.count()).select_from(AuditEvent)) or 0)
    recent = int(
        await session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.occurred_at >= week)
        )
        or 0
    )
    denials = int(
        await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.occurred_at >= week,
                AuditEvent.action.in_(
                    [AuditAction.AUTHZ_DENIED, AuditAction.AUTHZ_DENIALS_SUPPRESSED]
                ),
            )
        )
        or 0
    )
    latest = list(
        await session.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(LIST))
    )
    return Section(
        key="audit_log",
        count=total,
        href="/audit",
        items=[
            Item(
                kind="audit_event",
                id=str(e.seq),
                label=e.action,
                href=(
                    f"/audit?entity_type={e.entity_type}&entity_id={e.entity_id}"
                    if e.entity_type and e.entity_id
                    else f"/audit?action={e.action}"
                ),
                at=e.occurred_at,
                detail={"actor_type": e.actor_type, "workflow": e.workflow},
            )
            for e in latest
        ],
        stats=[
            Stat("events_total", float(total), href="/audit"),
            Stat("events_7d", float(recent), href="/audit"),
            Stat("denials_7d", float(denials), href=f"/audit?action={AuditAction.AUTHZ_DENIED}"),
        ],
    )


async def _integrity(
    session: AsyncSession,
    actor: Actor,
    *,
    anchor_store: AnchorStore | None,
    anchor_public_key: Ed25519PublicKey | None,
    large_chain_events: int,
) -> tuple[Section, Section, bool]:
    """(integrity, anchors, broken). The chain is checked inline when it is short."""
    keys = public_keys(anchor_public_key) if anchor_public_key is not None else None
    heads = None
    anchor_note: str | None = None
    if anchor_store is None or keys is None:
        anchor_note = "not_configured"
    else:
        try:
            heads = await load_anchors(anchor_store, actor.org_id, keys)
        except AnchorError:
            anchor_note = "anchor_invalid"
        except AnchorStoreError:
            anchor_note = "unavailable"

    length = await chain_length(session)
    stats: list[Stat] = [Stat("chain_events", float(length), href="/audit")]
    items: list[Item] = []
    broken = False
    source: str
    if length <= large_chain_events:
        loader = None
        if heads is not None:
            known = anchor_map(heads)

            async def loader_fn(_org: object) -> dict[int, str]:
                return dict(known)

            loader = loader_fn
        result, info = await verify_with_anchors(session, actor.org_id, loader)
        broken = not result.ok
        source = "checked_now"
        items.append(
            Item(
                kind="verification",
                id="now",
                label="chain",
                href="/audit",
                status="ok" if result.ok else "broken",
                detail={
                    "checked": result.checked,
                    "last_seq": result.last_seq,
                    "broken_seq": result.broken_seq,
                    "reason": result.reason,
                    "anchors_checked": bool(info.get("anchors_checked")),
                    "anchored_seq": info.get("anchored_seq"),
                },
            )
        )
    else:
        source = "latest_stored_verification"
        row = await session.scalar(
            select(AuditVerification)
            .where(AuditVerification.status == "done")
            .order_by(AuditVerification.completed_at.desc())
            .limit(1)
        )
        if row is not None and row.result is not None:
            ok = bool(row.result.get("ok"))
            broken = not ok
            items.append(
                Item(
                    kind="verification",
                    id=str(row.id),
                    label="chain",
                    href="/audit",
                    at=row.completed_at,
                    status="ok" if ok else "broken",
                    detail={
                        "checked": row.result.get("checked"),
                        "last_seq": row.result.get("last_seq"),
                        "broken_seq": row.result.get("broken_seq"),
                        "reason": row.result.get("reason"),
                    },
                )
            )
    failures = list(
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == AuditAction.AUDIT_CHAIN_BROKEN)
            .order_by(AuditEvent.seq.desc())
            .limit(LIST)
        )
    )
    if failures:
        broken = True
    stats.append(
        Stat(
            "nightly_failures",
            float(len(failures)),
            href=f"/audit?action={AuditAction.AUDIT_CHAIN_BROKEN}",
        )
    )
    integrity = Section(
        key="integrity",
        count=None if not items else (1 if broken else 0),
        href="/audit",
        items=items,
        stats=stats,
        source=source,
        note=None if items else "no_data",
    )
    anchors = Section(
        key="anchors",
        count=len(heads) if heads is not None else None,
        href="/audit",
        items=[
            Item(
                kind="anchor",
                id=str(h.seq),
                label=h.hash[:16],
                href="/audit",
                status="verified",
                detail={"seq": h.seq, "key_id": h.key_id, "anchored_at": h.anchored_at},
            )
            for h in list(reversed(heads or []))[:LIST]
        ],
        note=anchor_note,
    )
    return integrity, anchors, broken or anchor_note == "anchor_invalid"


async def _approvals(session: AsyncSession, actor: Actor, now: datetime) -> Section:
    scope = read_scope(actor, "core.approval.read")
    since = now - timedelta(days=HISTORY_DAYS)
    rows = (
        await session.execute(
            select(ApprovalDecision, Approval)
            .join(Approval, Approval.id == ApprovalDecision.approval_id)
            .where(ApprovalDecision.created_at >= since, scope.clause(Approval.workflow))
            .order_by(ApprovalDecision.created_at.desc())
        )
    ).all()
    outcomes = Counter(d.decision for d, _ in rows)
    return section(
        "approvals_history",
        [
            Item(
                kind="approval_decision",
                id=str(d.id),
                label=a.title,
                href=f"/inbox/{a.id}",
                at=d.created_at,
                status=d.decision,
                detail={
                    "workflow": a.workflow,
                    "approval_type": a.approval_type,
                    "channel": d.channel,
                    "second_factor": d.second_factor,
                    "delegated": d.on_behalf_of is not None,
                },
            )
            for d, a in rows
        ],
        href="/inbox?status=all",
        stats=[
            Stat(f"decisions_{name}", float(n), href="/inbox?status=all")
            for name, n in sorted(outcomes.items())
        ],
    )


async def _overrides(session: AsyncSession, actor: Actor, now: datetime) -> Section:
    scope = read_scope(actor, "core.decision.read")
    earlier = aliased(DecisionRecord)
    human = [k.value for k in HUMAN_KINDS]
    since = now - timedelta(days=HISTORY_DAYS)
    rows = (
        await session.execute(
            select(DecisionRecord, earlier)
            .join(
                earlier,
                and_(
                    earlier.org_id == DecisionRecord.org_id,
                    earlier.id == DecisionRecord.supersedes,
                ),
            )
            .where(
                DecisionRecord.kind.in_(human),
                earlier.kind.not_in(human),
                DecisionRecord.decided_at >= since,
                scope.clause(DecisionRecord.workflow),
            )
            .order_by(DecisionRecord.decided_at.desc())
        )
    ).all()
    items = []
    for human_row, ai_row in rows[:LIST]:
        items.append(
            Item(
                kind="override",
                id=human_row.id,
                label=human_row.final_action or human_row.kind,
                href=f"/decisions/{human_row.id}",
                at=human_row.decided_at,
                status=human_row.kind,
                detail={
                    "workflow": human_row.workflow,
                    "superseded": ai_row.id,
                    "superseded_kind": ai_row.kind,
                    "actor_type": human_row.actor_type,
                    # Whether a reason was recorded; the reason is on the receipt.
                    "reason_recorded": human_row.sensitive is not None
                    and human_row.sensitive_erased_at is None,
                },
            )
        )
    return Section(
        key="overrides",
        count=len(rows),
        href="/audit?action=core.decision.recorded",
        items=items,
        note="reason_on_receipt",
    )


async def _ai_decisions(session: AsyncSession, actor: Actor, now: datetime) -> Section:
    scope = read_scope(actor, "core.decision.read")
    human = [k.value for k in HUMAN_KINDS]
    since = now - timedelta(days=HISTORY_DAYS)
    where = (
        DecisionRecord.kind.not_in(human),
        DecisionRecord.decided_at >= since,
        scope.clause(DecisionRecord.workflow),
    )
    total = int(
        await session.scalar(select(func.count()).select_from(DecisionRecord).where(*where)) or 0
    )
    rows = list(
        await session.scalars(
            select(DecisionRecord)
            .where(*where)
            .order_by(DecisionRecord.decided_at.desc())
            .limit(LIST)
        )
    )
    items = []
    for r in rows:
        prompts = ", ".join(
            f"{p.get('task')}@{p.get('version')}" for p in (r.prompt_versions or [])[:3]
        )
        items.append(
            Item(
                kind="decision",
                id=r.id,
                label=r.kind,
                href=f"/decisions/{r.id}",
                at=r.decided_at,
                detail={
                    "workflow": r.workflow,
                    "model": str(r.model.get("model")) if r.model.get("model") else None,
                    "provider": str(r.model.get("provider")) if r.model.get("provider") else None,
                    "prompts": prompts or None,
                    "confidence": r.confidence,
                },
            )
        )
    return Section(
        key="ai_decisions", count=total, href="/audit?entity_type=decision_record", items=items
    )


async def auditor_dashboard(
    session: AsyncSession,
    actor: Actor,
    *,
    now: datetime,
    sections: SectionRegistry,
    anchor_store: AnchorStore | None,
    anchor_public_key: Ed25519PublicKey | None,
    large_chain_events: int,
) -> Dashboard:
    integrity, anchors, broken = await _integrity(
        session,
        actor,
        anchor_store=anchor_store,
        anchor_public_key=anchor_public_key,
        large_chain_events=large_chain_events,
    )
    extra_action, extra = await plugin_sections(
        sections, Board.AUDITOR, SectionContext(session=session, actor=actor, now=now)
    )
    action: list[Section] = []
    if broken:
        action.append(
            Section(key="chain_alert", count=1, href="/audit", items=integrity.items, note="broken")
        )
    action.extend(extra_action)
    links = [Link("audit_search", "/audit")]
    if actor.has("core.audit.export"):
        links.append(Link("audit_export", "/audit#export"))
    return Dashboard(
        board=Board.AUDITOR,
        generated_at=now,
        action=action,
        sections=[
            integrity,
            anchors,
            await _audit_log(session, now),
            await _approvals(session, actor, now),
            await _overrides(session, actor, now),
            await _ai_decisions(session, actor, now),
            *extra,
        ],
        links=links,
    )
