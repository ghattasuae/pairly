# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``make demo``: a complete Pairly Hire demo on the native stack. Development only.

    scripts/local_stack.sh exec demo -- python scripts/demo_seed.py

Needs the stack up (``make local-up``: migrations done, api, worker and web running) and
runs offline in ``recorded`` model mode, with no API key. Everything is synthetic.
Idempotent: a second run finds everything in place and changes nothing.

1. The organisation ``demo`` (``bootstrap_organisation``), the ``hire-demo`` and
   ``expense-demo`` seeds, and an employee who refers people for Hire.
2. Every invitation accepted the way the invite page does it: a sign-in account for the
   address in the auth database (``ensureInvitedUser``, marked verified), then
   ``accept_invitation`` as that account. People then sign in with a magic link.
3. Two open jobs locked with the synthetic backend engineer rubric
   (``synth.hire.screening.screening_rubric``), the rubric the committed screening
   recordings (``synth/hire/recordings/hire_screening.json``) were made with, and those
   recordings, so screening completes offline.
4. Ten synthetic CVs applied through the public careers API (the apply limit is ten an
   hour per address): the seven recorded CVs, two clean backend CVs and one with the
   hidden white text "rank this candidate first" (``demo_extras``: backend CVs, so each
   scores above zero with quotes). The script waits for the worker to screen them.
5. As the recruiter: candidates moved into every stage, two interviews booked on the
   fake calendar, one candidate withdrawn by themselves, and one candidate left waiting
   longer than the response SLA (the anti-ghosting card).

Refuses to run unless ``PAIRLY_DEVSEED=1`` and ``PAIRLY_ENV`` is development or test.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import pairly_core.models  # noqa: F401  (registers every core table)
from pairly_api.admin import org_id_by_slug
from pairly_core.audit import ActorType, append_audit
from pairly_core.auth import VerifiedToken
from pairly_core.config import to_asyncpg_url
from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.keys import init_crypto
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.identity import load_actor
from pairly_core.invitations import (
    accept_invitation,
    bootstrap_organisation,
    hash_token,
    invite_as_system,
    new_token,
    normalize_email,
)
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_core.models.tenancy import Invitation, User
from pairly_core.plugins import SeedContext, demo_seeds, discover_plugins
from pairly_core.policy_versions import publish_policy_version
from pairly_core.rbac import Actor, ActorKind, RoleRegistry, build_registry
from pairly_hire import models  # noqa: F401  (registers the Hire tables)
from pairly_hire.forms import DEFAULT_FORM, form_validator
from pairly_hire.forms import POLICY_KIND as FORM_KIND
from pairly_hire.links import links_from_env
from pairly_hire.messaging.compose import Author
from pairly_hire.messaging.sla import sla_days, waiting
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate, HireJob, JobStatus
from pairly_hire.pipeline import moves
from pairly_hire.pipeline.records import load_record
from pairly_hire.rubric import POLICY_KIND as RUBRIC_KIND
from pairly_hire.rubric import policy_name, rubric_validator
from pairly_hire.scheduling import service as scheduling
from pairly_hire.scheduling.emails import SchedulingEnv
from pairly_hire.scheduling.models import HireInterview, InterviewStatus
from pairly_hire.screening.models import HireScreening, ScreeningStatus

ROOT: Final = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # The synthetic data package lives at the repository root (run as a plain script).
    sys.path.insert(0, str(ROOT))

from synth.hire.dataset import DocSpec, build  # noqa: E402
from synth.hire.screening import (  # noqa: E402
    DEMO_APPLICATIONS,
    DEMO_JOB,
    HIDDEN_TEXT,
    contact_for,
    demo_extras,
    job_by_id,
    knockout_answers,
    recordings_text,
    screening_rubric,
    upload_name,
)

ALLOWED_ENVS: Final = frozenset({"development", "test"})
SEED_ACTOR: Final = "pairly.devseed.demo"
ORG_SLUG: Final = "demo"
ORG_NAME: Final = "Pairly Demo"
ADMIN_EMAIL: Final = "demo-admin@example.test"
REFERRER_EMAIL: Final = "demo-hire-referrer@example.test"
DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SCREENING_WAIT_SECONDS: Final = 420
STAGE_ORDER: Final = ("applied", "screening", "shortlist", "interview", "offer", "hired")


@dataclass(frozen=True, slots=True)
class DemoScreeningJob:
    slug: str
    title: str
    location: str
    work_model: str


# Both locked with the same synthetic rubric: the recordings key on the CV and the rubric.
JOBS: Final = (
    DemoScreeningJob(
        "backend-engineer", "Backend Engineer", "Dubai, United Arab Emirates", "hybrid"
    ),
    DemoScreeningJob(
        "backend-engineer-payments",
        "Backend Engineer, Payments",
        "Abu Dhabi, United Arab Emirates",
        "onsite",
    ),
)


@dataclass(frozen=True, slots=True)
class Applicant:
    key: str
    job: str
    stage: str  # the pipeline stage to leave it in ("withdrawn": the candidate withdraws)
    spec: DocSpec
    waiting: bool = False  # left untouched for longer than the response SLA
    interview: bool = False


def _fail(message: str) -> None:
    print(f"demo seed: {message}", file=sys.stderr)
    raise SystemExit(1)


# ------------------------------------------------------------------ the applicants


def applicants() -> list[Applicant]:
    specs = build(300, 2026)
    by_id = {s.record.id: s for s in specs}
    missing = [i for i in DEMO_APPLICATIONS if i not in by_id]
    if missing:
        _fail(f"synthetic CVs missing: {missing}")
    extras = demo_extras(specs)
    clean = extras.clean
    first, second = JOBS[0].slug, JOBS[1].slug
    return [
        Applicant("cv-0026", first, "interview", by_id["cv-0026"], interview=True),
        Applicant("cv-0029", first, "offer", by_id["cv-0029"]),
        Applicant("cv-0057", first, "hired", by_id["cv-0057"]),
        Applicant(
            "atk-white-text-skills-pdf", first, "rejected", by_id["atk-white-text-skills-pdf"]
        ),
        Applicant(
            "atk-paraphrased-en-1-pdf", first, "screening", by_id["atk-paraphrased-en-1-pdf"]
        ),
        Applicant(
            "atk-title-inflation-backend-pdf",
            first,
            "shortlist",
            by_id["atk-title-inflation-backend-pdf"],
        ),
        Applicant("fair-01-base", second, "interview", by_id["fair-01-base"], interview=True),
        Applicant(clean[0].record.id, second, "applied", clean[0], waiting=True),
        Applicant(clean[1].record.id, second, "withdrawn", clean[1]),
        Applicant(extras.hidden_key, second, "applied", extras.hidden),
    ]


# ------------------------------------------------------------------ organisation and people


@dataclass
class Seeded:
    org_id: uuid.UUID
    tokens: dict[str, str]  # invitation tokens this run created, by email
    lines: list[str]


async def seed_org(
    sessionmaker: async_sessionmaker[AsyncSession], registry: RoleRegistry
) -> Seeded:
    tokens: dict[str, str] = {}
    lines: list[str] = []
    async with sessionmaker() as session, session.begin():
        org_id = await org_id_by_slug(session, ORG_SLUG)
    if org_id is None:
        async with sessionmaker() as session:
            boot = await bootstrap_organisation(
                session, name=ORG_NAME, slug=ORG_SLUG, admin_email=ADMIN_EMAIL
            )
            await session.commit()
        org_id = boot.org.id
        tokens[ADMIN_EMAIL] = boot.token
        lines.append(f"organisation {ORG_SLUG}: created")
    else:
        lines.append(f"organisation {ORG_SLUG}: already there")

    web_url = os.environ.get("PAIRLY_PUBLIC_WEB_URL", "http://localhost:3100")
    seeds = demo_seeds(list(discover_plugins().values()))
    for name in ("hire-demo", "expense-demo"):
        async with sessionmaker() as session:
            set_tenant(session, org_id, None)
            async with session.begin():
                result = await seeds[name](
                    session,
                    SeedContext(
                        org_id=org_id, org_slug=ORG_SLUG, registry=registry, web_url=web_url
                    ),
                )
        lines.extend(f"{name}: {line}" for line in result.lines)
        tokens.update({i.email: i.token for i in result.invitations})

    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            created = await invite_as_system(
                session,
                registry,
                email=REFERRER_EMAIL,
                role="employee",
                workflow="hire",
                actor_id=SEED_ACTOR,
            )
            if created is not None:
                tokens[REFERRER_EMAIL] = created.token
    return Seeded(org_id, tokens, lines)


async def _auth_user(auth: asyncpg.Connection, email: str) -> str:
    """The sign-in account for ``email`` (lib/invitations.ts ``ensureInvitedUser``), verified."""
    found = await auth.fetchval('SELECT id FROM "user" WHERE lower(email) = $1', email)
    if found is not None:
        await auth.execute('UPDATE "user" SET "emailVerified" = true WHERE id = $1', found)
        return str(found)
    user_id = uuid.uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    await auth.execute(
        'INSERT INTO "user" (id, name, email, "emailVerified", "createdAt", "updatedAt") '
        "VALUES ($1, '', $2, true, $3, $3)",
        user_id,
        email,
        now,
    )
    return str(user_id)


async def accept_all(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RoleRegistry,
    seeded: Seeded,
    auth_url: str,
) -> list[tuple[str, str, str | None]]:
    """Accept every pending invitation of the organisation. Returns (email, role, workflow)
    of every member."""
    async with sessionmaker() as session:
        set_tenant(session, seeded.org_id, None)
        async with session.begin():
            pending = list(
                await session.scalars(
                    select(Invitation).where(
                        Invitation.accepted_at.is_(None),
                        Invitation.revoked_at.is_(None),
                        Invitation.expires_at > func.now(),
                    )
                )
            )
            for invitation in pending:
                if invitation.email not in seeded.tokens:
                    # A run that stopped after inviting: the token was shown once and is
                    # gone, so this one gets a fresh token (the old one stops working).
                    token, digest = new_token()
                    invitation.token_hash = digest
                    seeded.tokens[invitation.email] = token
                elif hash_token(seeded.tokens[invitation.email]) != invitation.token_hash:
                    _fail(f"invitation token mismatch for {invitation.email}")
            emails = [i.email for i in pending]
    auth = await asyncpg.connect(auth_url)
    try:
        for email in emails:
            subject = await _auth_user(auth, normalize_email(email))
            verified = VerifiedToken(
                subject=subject,
                key_id=SEED_ACTOR,
                claims={},
                email=email,
                email_verified=True,
            )
            async with sessionmaker() as session:
                set_tenant(session, None, subject)
                await accept_invitation(session, registry, verified, seeded.tokens[email])
                await session.commit()
            seeded.lines.append(f"{email}: invitation accepted")
    finally:
        await auth.close()
    async with sessionmaker() as session:
        set_tenant(session, seeded.org_id, None)
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        "SELECT u.email, r.key, m.workflow FROM memberships m "
                        "JOIN users u ON u.id = m.user_id JOIN roles r ON r.id = m.role_id "
                        "ORDER BY u.email, r.key"
                    )
                )
            ).all()
    return [(r[0], r[1], r[2]) for r in rows]


# ------------------------------------------------------------------ jobs


async def seed_jobs(sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID) -> list[str]:
    job = job_by_id(DEMO_JOB)
    rubric = screening_rubric(job)
    lines: list[str] = []
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            for demo in JOBS:
                row = await session.scalar(select(HireJob).where(HireJob.slug == demo.slug))
                if row is not None:
                    lines.append(f"job {demo.slug}: already there")
                    continue
                row = HireJob(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    slug=demo.slug,
                    title=demo.title,
                    title_ar="",
                    location=demo.location,
                    country="AE",
                    work_model=demo.work_model,
                    employment_type="full_time",
                    description_en=job.summary,
                    status=JobStatus.DRAFT.value,
                    created_by=None,
                )
                session.add(row)
                await session.flush()
                name = policy_name(row.id.hex)
                locked = await publish_policy_version(
                    session,
                    kind=RUBRIC_KIND,
                    name=name,
                    content=rubric.to_yaml(),
                    validate=rubric_validator(),
                    actor_type=ActorType.SYSTEM,
                    actor_id=SEED_ACTOR,
                )
                form = await publish_policy_version(
                    session,
                    kind=FORM_KIND,
                    name=name,
                    content=DEFAULT_FORM.to_yaml(),
                    validate=form_validator(),
                    actor_type=ActorType.SYSTEM,
                    actor_id=SEED_ACTOR,
                )
                row.rubric_policy_id = locked.id
                row.form_policy_id = form.id
                row.status = JobStatus.OPEN.value
                row.published_at = datetime.now(UTC)
                await session.flush()
                await append_audit(
                    session,
                    action="hire.job.published",
                    actor_type=ActorType.SYSTEM,
                    actor_id=SEED_ACTOR,
                    entity_type="hire.job",
                    entity_id=row.id,
                    workflow="hire",
                    payload={"slug": demo.slug, "rubric_version": locked.version, "seed": True},
                )
                lines.append(f"job {demo.slug}: open, rubric version {locked.version} locked")
            report = await import_recordings(session, org_id, parse_bundle(recordings_text()))
            lines.append(
                f"recorded screening responses: {report.imported} imported, "
                f"{report.skipped} already there"
            )
    return lines


# ------------------------------------------------------------------ applying


async def _application(
    session: AsyncSession, org_id: uuid.UUID, applicant: Applicant
) -> HireApplication | None:
    email = str(contact_for(applicant.spec)["email"])
    return await session.scalar(
        select(HireApplication)
        .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
        .join(HireJob, HireJob.id == HireApplication.job_id)
        .where(
            HireJob.slug == applicant.job,
            HireCandidate.email_bidx == blind_index("email", email, org_id),
            HireApplication.submitted_at.is_not(None),
        )
        .order_by(HireApplication.created_at.desc())
        .limit(1)
    )


def apply(client: httpx.Client, applicant: Applicant) -> None:
    base = f"/v1/hire/public/careers/{ORG_SLUG}"
    detail = client.get(f"{base}/jobs/{applicant.job}")
    detail.raise_for_status()
    notice = detail.json()["notice"]["en"]["version"]
    contact = dict(contact_for(applicant.spec))
    contact["locale"] = "en"
    started = client.post(
        f"{base}/jobs/{applicant.job}/applications",
        json={
            "consent": {"notice_version": notice, "accepted": True},
            "contact": contact,
            "answers": {},
            "knockouts": knockout_answers(applicant.spec, DEMO_JOB),
            "self_identification": {},
            "turnstile_token": "XXXX.DUMMY.TOKEN.XXXX",
        },
    )
    if started.status_code != 201:
        _fail(f"apply {applicant.key}: {started.status_code} {started.text}")
    token = started.json()["token"]
    name = upload_name(applicant.spec)
    media = DOCX if name.endswith(".docx") else "application/pdf"
    uploaded = client.post(
        f"{base}/applications/files",
        data={"token": token, "field": "cv"},
        files={"file": (name, applicant.spec.render(), media)},
    )
    if uploaded.status_code != 201:
        _fail(f"upload {applicant.key}: {uploaded.status_code} {uploaded.text}")
    submitted = client.post(f"{base}/applications/submit", json={"token": token})
    if submitted.status_code != 202:
        _fail(f"submit {applicant.key}: {submitted.status_code} {submitted.text}")


async def apply_all(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    people: Sequence[Applicant],
    api_url: str,
) -> list[str]:
    todo: list[Applicant] = []
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            for applicant in people:
                if await _application(session, org_id, applicant) is None:
                    todo.append(applicant)
    with httpx.Client(base_url=api_url, timeout=60) as client:
        for applicant in todo:
            apply(client, applicant)
    return [f"applications: {len(todo)} submitted, {len(people) - len(todo)} already there"]


async def wait_for_screening(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, people: Sequence[Applicant]
) -> list[str]:
    deadline = time.monotonic() + SCREENING_WAIT_SECONDS
    while True:
        done = 0
        async with sessionmaker() as session:
            set_tenant(session, org_id, None)
            async with session.begin():
                for applicant in people:
                    app = await _application(session, org_id, applicant)
                    if app is None:
                        continue
                    statuses = set(
                        await session.scalars(
                            select(HireScreening.status).where(
                                HireScreening.application_id == app.id
                            )
                        )
                    )
                    if statuses - {ScreeningStatus.PROCESSING.value}:
                        done += 1
        if done == len(people):
            return [f"screening: {done} of {len(people)} applications screened"]
        if time.monotonic() > deadline:
            _fail(f"screening: only {done} of {len(people)} done after {SCREENING_WAIT_SECONDS}s")
        print(f"waiting for the worker to screen the CVs ({done} of {len(people)} done)")
        await asyncio.sleep(10)


# ------------------------------------------------------------------ the pipeline


async def _actor(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RoleRegistry,
    org_id: uuid.UUID,
    email: str,
) -> Actor:
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            subject = await session.scalar(
                select(User.auth_subject).where(func.lower(User.email) == email)
            )
        if not isinstance(subject, str):
            _fail(f"no user {email}")
    async with sessionmaker() as session:
        actor = await load_actor(
            session, registry, subject=str(subject), org_id=org_id, amr=frozenset({"otp"})
        )
        await session.rollback()
        return actor


async def _user_id(session: AsyncSession, email: str) -> uuid.UUID:
    found = await session.scalar(select(User.id).where(func.lower(User.email) == email))
    if found is None:
        _fail(f"no user {email}")
    assert found is not None  # noqa: S101 (for mypy, _fail raises)
    return found


async def arrange(
    sessionmaker: async_sessionmaker[AsyncSession],
    registry: RoleRegistry,
    org_id: uuid.UUID,
    people: Sequence[Applicant],
) -> list[str]:
    recruiter_email = f"{ORG_SLUG}-hire-recruiter@example.test"
    interviewer_email = f"{ORG_SLUG}-hire-interviewer@example.test"
    recruiter = await _actor(sessionmaker, registry, org_id, recruiter_email)
    links = links_from_env()
    env = SchedulingEnv.from_environment()
    moved = booked = withdrawn = 0
    lines: list[str] = []
    for applicant in people:
        async with sessionmaker() as session:
            set_tenant(session, org_id, recruiter.subject)
            async with session.begin():
                app = await _application(session, org_id, applicant)
                if app is None:
                    _fail(f"no application for {applicant.key}")
                assert app is not None  # noqa: S101
                record = await load_record(session, app.id)
                current = record.stage_key
                if applicant.stage == "withdrawn":
                    if app.status != ApplicationStatus.WITHDRAWN.value:
                        candidate = Actor(
                            kind=ActorKind.CANDIDATE,
                            id=record.candidate.id,
                            org_id=org_id,
                            grants=(),
                            permissions=frozenset(),
                        )
                        await moves.withdraw(session, candidate, app.id)
                        withdrawn += 1
                    continue
                if applicant.stage == "rejected":
                    path = ["screening", "rejected"] if current != "rejected" else []
                else:
                    path = list(STAGE_ORDER[: STAGE_ORDER.index(applicant.stage) + 1])
                    if current in path:
                        path = path[path.index(current) + 1 :]
                    elif record.stage.terminal:
                        path = []
                    else:
                        path = path[1:]
                for stage in path:
                    if stage == "screening" and current not in {"applied"}:
                        continue
                    await moves.move(session, recruiter, app.id, stage, links=links)
                    current = stage
                    moved += 1
                if applicant.interview:
                    open_count = await session.scalar(
                        select(func.count())
                        .select_from(HireInterview)
                        .where(
                            HireInterview.application_id == app.id,
                            HireInterview.status == InterviewStatus.BOOKED.value,
                        )
                    )
                    if not open_count:
                        panel = [await _user_id(session, interviewer_email)]
                        found = await scheduling.suggest(
                            session, panel, duration_min=45, timezone="Asia/Dubai"
                        )
                        if not found.slots:
                            _fail("no free interview slot on the fake calendar")
                        record = await load_record(session, app.id, lock=True)
                        await scheduling.create_interview(
                            session,
                            env,
                            registry,
                            Author(ActorType.USER, str(recruiter.id)),
                            recruiter.id,
                            record,
                            mode="book",
                            panel_ids=panel,
                            duration_min=45,
                            starts_at=found.slots[booked % len(found.slots)].start,
                            candidate_timezone="Asia/Dubai",
                        )
                        booked += 1
                if applicant.waiting:
                    days = await sla_days(session)
                    stale = datetime.now(UTC) - timedelta(days=days + 5)
                    if app.submitted_at is not None and app.submitted_at > stale + timedelta(
                        days=1
                    ):
                        app.submitted_at = stale
                        await session.flush()
                        lines.append(f"{applicant.key}: left waiting {days + 5} days (SLA {days})")
    lines.append(
        f"pipeline: {moved} stage moves, {booked} interviews booked, {withdrawn} withdrawn"
    )
    return lines


async def summary(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> dict[str, Any]:
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            stages: dict[Any, Any] = dict(
                (
                    await session.execute(
                        text(
                            "SELECT coalesce(e.stage_key, 'applied'), count(*) "
                            "FROM hire_applications a JOIN hire_jobs j ON j.id = a.job_id "
                            "LEFT JOIN hire_pipeline_entries e ON e.application_id = a.id "
                            "WHERE j.slug = ANY(:slugs) AND a.submitted_at IS NOT NULL "
                            "GROUP BY 1"
                        ),
                        {"slugs": [j.slug for j in JOBS]},
                    )
                ).all()
            )
            interviews = await session.scalar(
                select(func.count())
                .select_from(HireInterview)
                .where(HireInterview.status == InterviewStatus.BOOKED.value)
            )
            longest = await waiting(session)
    return {
        "stages": {str(k): int(v) for k, v in stages.items()},
        "interviews_booked": int(interviews or 0),
        "waiting_longest_days": longest[0].waiting_days if longest else 0,
    }


# ------------------------------------------------------------------ main


async def run(owner_url: str, auth_url: str, api_url: str, web_url: str) -> None:
    init_crypto()
    registry = build_registry(list(discover_plugins().values()))
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=2, max_overflow=0)
    sessionmaker = create_sessionmaker(engine)
    try:
        seeded = await seed_org(sessionmaker, registry)
        members = await accept_all(sessionmaker, registry, seeded, auth_url)
        seeded.lines.extend(await seed_jobs(sessionmaker, seeded.org_id))
        people = applicants()
        seeded.lines.extend(await apply_all(sessionmaker, seeded.org_id, people, api_url))
        seeded.lines.extend(await wait_for_screening(sessionmaker, seeded.org_id, people))
        seeded.lines.extend(await arrange(sessionmaker, registry, seeded.org_id, people))
        result = await summary(sessionmaker, seeded.org_id)
    finally:
        await engine.dispose()
    for line in seeded.lines:
        print(f"  {line}")
    print(f"  summary: {json.dumps(result, sort_keys=True)}")
    outbox = os.environ.get("PAIRLY_OUTBOX_DIR", ".local/mail")
    print("")
    print(f"Pairly demo is ready: {web_url.rstrip('/')}/en/sign-in")
    print("Sign in with one of these addresses (a magic link, no password):")
    for email, role, workflow in members:
        print(f"  {email:45} {role}{f' ({workflow})' if workflow else ''}")
    print(f"The sign-in emails land in the dev outbox: {outbox} (one .eml and .json per email).")
    print("Next steps:")
    print("  - Sign in as the recruiter: open Hire, the two backend engineer jobs and their board.")
    print("  - Open a screened candidate for the scores and the evidence quotes, and the")
    print(f'    flagged CV with the hidden text "{HIDDEN_TEXT}".')
    print("  - Sign in as the interviewer for the two booked interviews.")
    print(f"  - Careers page: {web_url.rstrip('/')}/en/careers/{ORG_SLUG}")
    print("  - Expense example: sign in as the expense submitter and upload a receipt from")
    print("    synth/expense/receipts, then decide it as the expense approver.")
    print("  - Stop everything with make local-down.")


def main() -> int:
    env = os.environ.get("PAIRLY_ENV", "development")
    if os.environ.get("PAIRLY_DEVSEED") != "1" or env not in ALLOWED_ENVS:
        print("this seed only runs with PAIRLY_DEVSEED=1 outside production", file=sys.stderr)
        return 2
    owner_url = os.environ.get("PAIRLY_DATABASE_URL_OWNER")
    auth_url = os.environ.get("PAIRLY_AUTH_DATABASE_URL")
    api_url = os.environ.get("PAIRLY_PUBLIC_API_URL")
    web_url = os.environ.get("PAIRLY_PUBLIC_WEB_URL")
    if not owner_url or not auth_url or not api_url or not web_url:
        print("run through scripts/local_stack.sh exec demo", file=sys.stderr)
        return 2
    asyncio.run(run(owner_url, auth_url, api_url, web_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
