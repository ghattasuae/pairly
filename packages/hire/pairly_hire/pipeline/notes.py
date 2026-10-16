# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Notes with @mentions, and tags (brief 3.4.2).

* ``add_note``: a person of the hiring team (``hire.note.write``) writes a note on an
  application. The note is encrypted and never changed. People it mentions must be
  members who can read candidates (``hire.candidate.read``); each gets one in-app
  notification (``hire.note.mention``) naming the candidate the way they may see them
  (blind mode hides the name until the stage reveals it), never the note itself.
* ``set_tags``: replace an application's tags (short labels, at most ``MAX_TAGS``).
* ``hiring_team``: the members a note can mention.

Both are audited without their text (the audit trail holds no free text about people).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.service import member_permissions
from pairly_core.audit import audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ForbiddenError
from pairly_core.models import Membership, User
from pairly_core.notifications.service import Extras, Recipient, notify
from pairly_core.rbac import Actor, ActorKind, RoleRegistry, authorize
from pairly_hire.messaging.library import NOTE_MENTION
from pairly_hire.permissions import CANDIDATE_READ, NOTE_WRITE
from pairly_hire.pipeline.hooks import application_blind
from pairly_hire.pipeline.models import HireNote
from pairly_hire.pipeline.records import ensure_entry, load_record

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.application"
MAX_NOTE: Final = 4000
MAX_MENTIONS: Final = 10
MAX_TAGS: Final = 12
TAG_RE: Final = re.compile(r"^[\w][\w \-./+#]{0,31}$", re.UNICODE)


def _person(actor: Actor) -> None:
    authorize(actor, NOTE_WRITE)
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can write notes and tags")


@dataclass(frozen=True, slots=True)
class TeamMember:
    id: uuid.UUID
    name: str
    email: str


async def hiring_team(session: AsyncSession, registry: RoleRegistry) -> list[TeamMember]:
    """Members who can read candidates (the people a note may mention)."""
    rows = (
        await session.execute(
            select(User.id, User.display_name, User.email)
            .join(Membership, Membership.user_id == User.id)
            .distinct()
        )
    ).all()
    perms = await member_permissions(session, registry, [r.id for r in rows])
    team = [
        TeamMember(r.id, (r.display_name or r.email.split("@", 1)[0]).strip(), r.email)
        for r in rows
        if CANDIDATE_READ in perms.get(r.id, frozenset())
    ]
    return sorted(team, key=lambda m: (m.name.casefold(), str(m.id)))


@dataclass(frozen=True, slots=True)
class AddedNote:
    note: HireNote
    mentioned: list[uuid.UUID]


async def add_note(
    session: AsyncSession,
    actor: Actor,
    registry: RoleRegistry,
    application_id: uuid.UUID,
    *,
    body: str,
    mentions: list[uuid.UUID],
    link: str | None,
) -> AddedNote:
    _person(actor)
    text = body.strip()
    if not text or len(text) > MAX_NOTE:
        raise BadRequestError(f"a note has 1 to {MAX_NOTE} characters")
    wanted = list(dict.fromkeys(m for m in mentions if m != actor.id))
    if len(wanted) > MAX_MENTIONS:
        raise BadRequestError(f"mention at most {MAX_MENTIONS} people")
    perms = await member_permissions(session, registry, wanted)
    if any(CANDIDATE_READ not in perms.get(m, frozenset()) for m in wanted):
        raise BadRequestError("you can only mention people who can see candidates")
    record = await load_record(session, application_id)
    now = datetime.now(UTC)
    note = HireNote(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        author_id=actor.id,
        mentions=[str(m) for m in wanted],
        created_at=now,
    )
    setattr(note, "body", seal(HireNote, "body", text, org_id=note.org_id, row_id=note.id))  # noqa: B010
    session.add(note)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.note.added",
        entity_type=ENTITY,
        entity_id=record.app.id,
        workflow=WORKFLOW,
        payload={"note_id": str(note.id), "mentions": [str(m) for m in wanted]},
    )
    blind = await application_blind(session, record.app.id)
    for user_id in wanted:
        await notify(
            session,
            recipient=Recipient.user(user_id),
            template_key=NOTE_MENTION,
            variables={"candidate_label": record.label(blind), "job_title": record.job.title},
            dedupe_key=f"hire.mention:{note.id}:{user_id}",
            channels=["in_app"],
            extras=Extras(link=link),
            workflow=WORKFLOW,
        )
    return AddedNote(note, wanted)


def clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        value = " ".join(str(tag).split())
        if not value:
            continue
        if not TAG_RE.fullmatch(value):
            raise BadRequestError("a tag is a short label of letters, numbers and spaces")
        if value.casefold() not in {t.casefold() for t in out}:
            out.append(value)
    if len(out) > MAX_TAGS:
        raise BadRequestError(f"at most {MAX_TAGS} tags")
    return out


async def set_tags(
    session: AsyncSession, actor: Actor, application_id: uuid.UUID, tags: list[str]
) -> list[str]:
    _person(actor)
    clean = clean_tags(tags)
    record = await load_record(session, application_id, lock=True)
    entry = await ensure_entry(session, record)
    previous = list(entry.tags or [])
    if previous == clean:
        return clean
    entry.tags = clean
    entry.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.candidate.tagged",
        entity_type=ENTITY,
        entity_id=record.app.id,
        workflow=WORKFLOW,
        payload={"count": len(clean)},
    )
    return clean
