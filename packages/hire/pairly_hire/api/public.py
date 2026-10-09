# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Public careers routes, no sign-in, under ``/v1/hire/public/careers/{org_slug}``.

Reads (``Cache-Control: public, max-age=300``; 120 requests a minute per IP):

* ``GET /{org_slug}``: the organisation's name and its open jobs.
* ``GET /{org_slug}/jobs/{job_slug}``: one open job: text in English and Arabic, the
  salary when shown, the ``JobPosting`` JSON-LD, the AI-use notice, and the public
  application form (questions, knockout questions from the locked rubric without the
  expected answers, file fields, self-identification categories and options).
* ``GET /{org_slug}/feed.json`` and ``/feed.xml``: open jobs for job boards.

The apply flow (``pairly_hire.applying``; ``Cache-Control: no-store``):

* ``POST /{org_slug}/jobs/{job_slug}/applications`` (JSON, 201): consent, contact
  details, answers, voluntary self-identification, the Turnstile token and an optional
  employee referral token (``referral``, recorded when valid). Returns a
  one-time upload token (2 hours). 10 per hour per IP; 5 per day per email address.
* ``POST /{org_slug}/applications/files`` (multipart: ``file``, ``token``, ``field``,
  201): one file per request through the engine's ingestion (workflow ``hire``). 40
  per hour per IP.
* ``POST /{org_slug}/applications/submit`` (JSON ``{"token"}``, 202): the CV must be
  there. Queues the acknowledgement email. 20 per hour per IP. After the response, the
  screening starts (``pairly_hire.screening.start``; a person always decides).

Tenant handling: the slug is resolved by ``hire_careers_org(slug)`` (SECURITY DEFINER,
returns id and name only, and only for an active organisation whose careers page is
visible); that transaction is committed, then the session is bound to the organisation
and everything else runs under Row Level Security. An unknown slug, a hidden careers
page and a job that is not open all answer the same 404.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_api.problems import problem_response
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload_with_fields,
    rejected_response,
)
from pairly_core.db import set_tenant
from pairly_core.errors import BadRequestError, NotFoundError, PairlyError
from pairly_core.identity import RequestSession, get_context
from pairly_core.ingestion.models import DocumentSource
from pairly_core.ingestion.service import (
    AuditBy,
    RejectReason,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_hire.antispam import SlidingWindowLimiter, client_ip, verifier_for
from pairly_hire.api.common import web_url
from pairly_hire.applying import (
    SYSTEM_ACTOR,
    ApplyRequest,
    Contact,
    application_for_token,
    attach_file,
    file_fields,
    start_application,
    submit_application,
)
from pairly_hire.careers import (
    CareersOrg,
    careers_org,
    job_posting_ld,
    job_url,
    json_feed,
    public_job,
    xml_feed,
)
from pairly_hire.jobs import current_form, org_settings
from pairly_hire.links import links_from_settings
from pairly_hire.models import HireJob, JobStatus
from pairly_hire.notice import CURRENT_VERSION, NOTICES
from pairly_hire.pipeline.referrals import attach_referral
from pairly_hire.rubrics import locked_rubric
from pairly_hire.screening.start import start_after_submit
from pairly_hire.selfid.schema import CATEGORIES

router = APIRouter(prefix="/public/careers", tags=["hire-public"])

WORKFLOW: Final = "hire"
PUBLIC_CACHE: Final = "public, max-age=300, stale-while-revalidate=600"
NO_STORE: Final = "no-store"

# (requests, seconds) per IP.
LIMITS: Final = {
    "read": (120, 60.0),
    "apply": (10, 3600.0),
    "files": (40, 3600.0),
    "submit": (20, 3600.0),
}


def _notifications(request: Request) -> Any:
    return getattr(request.app.state, "notifications", None)


def _limiter(request: Request, kind: str) -> SlidingWindowLimiter:
    """Per-process limiters, one set per app (so each test app starts fresh)."""
    store: dict[str, SlidingWindowLimiter] | None = getattr(
        request.app.state, "hire_limiters", None
    )
    if store is None:
        store = {k: SlidingWindowLimiter(n, w) for k, (n, w) in LIMITS.items()}
        request.app.state.hire_limiters = store
    return store[kind]


def _limit(request: Request, kind: str, org_slug: str) -> str:
    ip = client_ip(request)
    _limiter(request, kind).hit(f"{ip}|{org_slug}" if kind != "read" else ip)
    return ip


async def _bind(session: AsyncSession, org_slug: str) -> CareersOrg:
    org = await careers_org(session, org_slug)
    await session.commit()
    if org is None:
        raise NotFoundError("careers page not found")
    set_tenant(session, org.id, None)
    return org


async def _open_job(session: AsyncSession, job_slug: str) -> HireJob:
    job = await session.scalar(
        select(HireJob).where(HireJob.slug == job_slug, HireJob.status == JobStatus.OPEN.value)
    )
    if job is None:
        raise NotFoundError("job not found")
    return job


MAX_APPLY_BYTES: Final = 64 * 1024


async def _json_body[T: BaseModel](request: Request, model: type[T], limit: int) -> T:
    """Read a small JSON body ourselves, refusing anything over ``limit`` bytes before it
    is parsed (FastAPI would read a body of any size first), then validate it."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise PayloadTooLargeError()
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > limit:
            raise PayloadTooLargeError()
    try:
        return model.model_validate_json(bytes(received))
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""
        message = str(errors[0]["msg"]) if errors else "invalid body"
        raise BadRequestError(
            f"invalid request: {where}: {message}" if where else message
        ) from None


class PayloadTooLargeError(PairlyError):
    status = 413
    code = "payload_too_large"
    title = "Request too large"


def _cached(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": PUBLIC_CACHE})


def _no_store(body: Any, status: int) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": NO_STORE})


# ------------------------------------------------------------------ schemas (docs)


class HirePublicSalary(BaseModel):
    min: str
    max: str
    currency: str | None
    period: str | None


class HirePublicJob(BaseModel):
    slug: str
    title: str
    title_ar: str
    team: str | None
    location: str
    country: str | None
    work_model: str
    employment_type: str
    salary: HirePublicSalary | None
    published_at: str
    url: str


class HirePublicOrg(BaseModel):
    name: str
    slug: str


class HireCareersOut(BaseModel):
    organisation: HirePublicOrg
    jobs: list[HirePublicJob]


class HirePublicNotice(BaseModel):
    version: str
    title: str
    points: list[str]
    consent: str


class HirePublicJobDetail(HirePublicJob):
    organisation: HirePublicOrg
    description_en: str
    description_ar: str
    json_ld: dict[str, Any]
    notice: dict[str, HirePublicNotice]
    form: dict[str, Any]


class HireApplyContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, StringConstraints(max_length=200)]
    email: Annotated[str, StringConstraints(max_length=320)]
    phone: Annotated[str, StringConstraints(max_length=60)] | None = None
    location: Annotated[str, StringConstraints(max_length=200)] | None = None
    profile_url: Annotated[str, StringConstraints(max_length=400)] | None = None
    locale: Annotated[str, StringConstraints(pattern=r"^(en|ar)$")] = "en"


class HireApplyConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notice_version: Annotated[str, StringConstraints(max_length=32)]
    accepted: bool


class HireApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consent: HireApplyConsent
    contact: HireApplyContact
    answers: dict[str, Any] = Field(default_factory=dict, max_length=40)
    knockouts: dict[str, Any] = Field(default_factory=dict, max_length=10)
    self_identification: dict[str, Any] = Field(default_factory=dict, max_length=10)
    turnstile_token: Annotated[str, StringConstraints(max_length=2048)]
    # The ``ref`` of an employee's referral link (``pairly_hire.pipeline.referrals``).
    # Invalid or foreign: ignored, the application goes on without a referral.
    referral: Annotated[str, StringConstraints(max_length=200)] | None = None


class HireApplyOut(BaseModel):
    token: str
    expires_at: datetime
    file_fields: list[str]
    required_files: list[str]


class HireTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, StringConstraints(min_length=10, max_length=100)]


class HireFileOut(BaseModel):
    field: str
    received: bool


class HireSubmitOut(BaseModel):
    status: str
    reference: str
    response_days: int


# ------------------------------------------------------------------ reads


@router.get("/{org_slug}", response_model=HireCareersOut)
async def careers(org_slug: str, request: Request, session: RequestSession) -> Any:
    _limit(request, "read", org_slug)
    org = await _bind(session, org_slug)
    locale = "ar" if request.query_params.get("locale") == "ar" else "en"
    jobs = await session.scalars(
        select(HireJob)
        .where(HireJob.status == JobStatus.OPEN.value)
        .order_by(HireJob.published_at.desc(), HireJob.id)
        .limit(500)
    )
    base = web_url(request)
    return _cached(
        {
            "organisation": {"name": org.name, "slug": org.slug},
            "jobs": [public_job(j, org, base, locale) for j in jobs],
        }
    )


async def _open_jobs(session: AsyncSession) -> list[HireJob]:
    rows = await session.scalars(
        select(HireJob)
        .where(HireJob.status == JobStatus.OPEN.value)
        .order_by(HireJob.published_at.desc(), HireJob.id)
        .limit(500)
    )
    return list(rows)


@router.get("/{org_slug}/feed.json")
async def feed_json(org_slug: str, request: Request, session: RequestSession) -> Any:
    _limit(request, "read", org_slug)
    org = await _bind(session, org_slug)
    jobs = await _open_jobs(session)
    return _cached(json_feed(org, jobs, web_url(request), datetime.now(UTC)))


@router.get("/{org_slug}/feed.xml", response_class=Response)
async def feed_xml(org_slug: str, request: Request, session: RequestSession) -> Response:
    _limit(request, "read", org_slug)
    org = await _bind(session, org_slug)
    jobs = await _open_jobs(session)
    return Response(
        xml_feed(org, jobs, web_url(request), datetime.now(UTC)),
        media_type="application/xml; charset=utf-8",
        headers={"Cache-Control": PUBLIC_CACHE},
    )


def _public_form(form: Any, knockouts: list[Any]) -> dict[str, Any]:
    active = form.self_identification.active
    return {
        "standard": form.standard.model_dump(mode="json"),
        "questions": [q.model_dump(mode="json") for q in form.questions],
        "knockout_questions": [
            {"id": k.id, "question": k.question, "question_ar": k.question_ar} for k in knockouts
        ],
        "files": [
            {"id": "cv", "label": "CV", "label_ar": "السيرة الذاتية", "required": True},
            *(
                {"id": f.id, "label": f.label, "label_ar": f.label_ar, "required": False}
                for f in form.files
            ),
        ],
        "self_identification": [
            {
                "id": key,
                "label": CATEGORIES[key].label_en,
                "label_ar": CATEGORIES[key].label_ar,
                "options": [
                    {"id": o.id, "label": o.label_en, "label_ar": o.label_ar}
                    for o in CATEGORIES[key].options
                ],
            }
            for key in active
        ],
    }


@router.get("/{org_slug}/jobs/{job_slug}", response_model=HirePublicJobDetail)
async def job_detail(
    org_slug: str, job_slug: str, request: Request, session: RequestSession
) -> Any:
    _limit(request, "read", org_slug)
    org = await _bind(session, org_slug)
    job = await _open_job(session, job_slug)
    form, _ = await current_form(session, job)
    _, rubric = await locked_rubric(session, job)
    base = web_url(request)
    locale = "ar" if request.query_params.get("locale") == "ar" else "en"
    notices = NOTICES[CURRENT_VERSION]
    return _cached(
        public_job(job, org, base, locale)
        | {
            "organisation": {"name": org.name, "slug": org.slug},
            "description_en": job.description_en,
            "description_ar": job.description_ar,
            "json_ld": job_posting_ld(job, org, job_url(base, locale, org.slug, job.slug)),
            "notice": {
                lang: {
                    "version": n.version,
                    "title": n.title,
                    "points": list(n.points),
                    "consent": n.consent,
                }
                for lang, n in notices.items()
            },
            "form": _public_form(form, list(rubric.knockout_questions)),
        }
    )


# ------------------------------------------------------------------ apply


@router.post(
    "/{org_slug}/jobs/{job_slug}/applications",
    status_code=201,
    response_model=HireApplyOut,
)
async def apply(org_slug: str, job_slug: str, request: Request, session: RequestSession) -> Any:
    ip = _limit(request, "apply", org_slug)
    body = await _json_body(request, HireApplyIn, MAX_APPLY_BYTES)
    verifier = verifier_for(request, get_context(request).settings.env)
    org = await _bind(session, org_slug)
    job = await _open_job(session, job_slug)
    started = await start_application(
        session,
        org_id=org.id,
        job=job,
        request=ApplyRequest(
            contact=Contact(**body.contact.model_dump()),
            questions=body.answers,
            knockouts=body.knockouts,
            self_identification=body.self_identification,
            notice_version=body.consent.notice_version,
            consent=body.consent.accepted,
            turnstile_token=body.turnstile_token,
        ),
        verifier=verifier,
        remote_ip=None if ip == "unknown" else ip,
    )
    await attach_referral(
        session,
        started.application,
        body.referral,
        links_from_settings(_notifications(request)).secret,
    )
    out = HireApplyOut(
        token=started.token,
        expires_at=started.expires_at,
        file_fields=sorted(started.files),
        required_files=["cv"],
    )
    return _no_store(out.model_dump(mode="json"), 201)


@router.post("/{org_slug}/applications/files", status_code=201, response_model=HireFileOut)
async def upload_file(
    org_slug: str,
    request: Request,
    session: RequestSession,
    background: BackgroundTasks,
) -> Any:
    _limit(request, "files", org_slug)
    services = get_services(request)
    policy = services.settings.policy_for(WORKFLOW)
    org = await _bind(session, org_slug)
    by = AuditBy.system(SYSTEM_ACTOR)
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        await record_rejection(
            session,
            by=by,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            reason=RejectReason.TOO_LARGE,
        )
        return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
    with tempfile.TemporaryDirectory(prefix="pairly-hire-", dir=services.tmp_dir) as tmp:
        try:
            received = await receive_upload_with_fields(
                request,
                Path(tmp),
                policy.max_bytes,
                services.settings.upload_timeout_seconds,
                text_fields=frozenset({"token", "field"}),
            )
        except UnsupportedBodyError:
            return problem_response(
                request,
                415,
                "unsupported_media_type",
                detail="send multipart/form-data with a 'file' part",
            )
        except TooLargeError:
            await record_rejection(
                session,
                by=by,
                workflow=WORKFLOW,
                source=DocumentSource.UPLOAD,
                reason=RejectReason.TOO_LARGE,
            )
            return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
        app = await application_for_token(session, received.fields.get("token", ""))
        field = received.fields.get("field", "cv").strip() or "cv"
        if field not in await file_fields(session, app):
            raise BadRequestError("this form has no such file field")
        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=received.file,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            filename=received.filename,
            policy=policy,
            by=by,
        )
    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, WORKFLOW)
    await attach_file(session, app, field, outcome.document, created=outcome.created)
    await session.commit()
    if outcome.created:
        # The worker scans and parses CVs like every other document.
        background.add_task(enqueue_scan, services.queue, org.id, outcome.document.id)
    return _no_store({"field": field, "received": True}, 201)


@router.post(
    "/{org_slug}/applications/submit",
    status_code=202,
    response_model=HireSubmitOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": HireTokenIn.model_json_schema()}},
        }
    },
)
async def submit(
    org_slug: str, request: Request, session: RequestSession, background: BackgroundTasks
) -> Any:
    _limit(request, "submit", org_slug)
    body = await _json_body(request, HireTokenIn, 1024)
    org = await _bind(session, org_slug)
    app = await application_for_token(session, body.token)
    job = await session.scalar(select(HireJob).where(HireJob.id == app.job_id))
    if job is None or job.status != JobStatus.OPEN.value:
        # The job closed or paused while the candidate was filling in the form.
        raise NotFoundError("job not found")
    settings = await org_settings(session)
    locale = app.consent_locale
    done = await submit_application(
        session,
        app,
        org_name=org.name,
        job_title=job.title_ar if locale == "ar" and job.title_ar else job.title,
        response_days=settings.response_days,
        # TODO(WP2.9): a magic link to the candidate portal instead of the careers page.
        portal_url=f"{web_url(request)}/{locale}/careers/{org.slug}",
    )
    out = HireSubmitOut(
        status=done.application.status,
        reference=done.application.reference,
        response_days=settings.response_days,
    )
    runtime = getattr(request.app.state, "workflow_runtime", None)
    if isinstance(runtime, WorkflowRuntime):
        # After the response: the screening run (pairly_hire.screening.start). When this
        # process cannot parse the CV itself, the worker job starts it once it is parsed.
        await session.commit()
        background.add_task(start_after_submit, runtime, org.id, done.application.id)
    return _no_store(out.model_dump(mode="json"), 202)
