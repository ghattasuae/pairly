# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/policies``: rule sets (policy as code) and policy documents for RAG (WP1.5).

Rule sets (``core.policy.read`` to read, ``core.policy.manage`` to publish):

* ``GET  /v1/policies/rule-sets``                   every rule set name with its latest
  version (no content)
* ``GET  /v1/policies/rule-sets/{name}``            latest version (or ``?version=n``)
* ``GET  /v1/policies/rule-sets/{name}/versions``   every version, newest first
* ``POST /v1/policies/rule-sets/{name}/versions``   publish a new version (never an edit)
* ``POST /v1/policies/rule-sets/dry-run``           evaluate sample facts against a
  published version or draft YAML; nothing is stored
* ``GET  /v1/policies/rule-results?evaluation_id=`` stored results of one evaluation

Policy documents (``core.policy.read`` / ``core.policy.manage``):

* ``POST /v1/policies/documents?name=&title=``      multipart upload (PDF, DOCX, text),
  through the normal ingestion path (workflow ``policy``), as the next version of
  ``name``. Indexing happens once the file is scanned and parsed.
* ``GET  /v1/policies/documents``                   list (``?name=`` filter)
* ``GET  /v1/policies/documents/{id}``              one
* ``POST /v1/policies/documents/{id}/index``        index now (409 while not parsed)
* ``POST /v1/policies/documents/{id}/archive``      stop using it (final)
* ``POST /v1/policies/documents/{id}/override``     override the guardrails of a
  ``needs_review`` document with a written reason. High-severity findings need two
  different admins: the first call records the request (202), the second approves it.
  Then the document indexes, with every flagged span still left out.
* ``POST /v1/policies/search``                      hybrid search over active versions

Every policy document runs through the guardrails before it is indexed. Its findings
come back as ``warnings`` to callers holding ``core.policy.manage`` (others see only
``warning_count``). Status ``needs_review`` (a high-severity instruction found in the
text) is not indexed and not active: upload a corrected file as a new version, or
override (see above).

``manage`` is a step-up action: it needs a TOTP sign-in. Another organisation's rows are
404 (Row Level Security hides them).
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pairly_api.problems import problem_response
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    DocumentOut,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload,
    rejected_response,
)
from pairly_core.audit import actor_ref
from pairly_core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from pairly_core.identity import OrgSession
from pairly_core.ingestion.models import Document, DocumentSource
from pairly_core.ingestion.service import (
    AuditBy,
    RejectReason,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.llm.settings import load_llm_settings
from pairly_core.policy.documents import (
    POLICY_KIND,
    POLICY_WORKFLOW,
    PolicyDocumentNotFoundError,
    active_policy_documents,
    archive_policy_document,
    get_policy_document,
    index_policy_document,
    list_policy_documents,
    override_needs_two_admins,
    policy_intake,
    register_policy_document,
    request_override,
)
from pairly_core.policy.models import PolicyDocument, RuleResult
from pairly_core.policy.permissions import POLICY_MANAGE, POLICY_READ
from pairly_core.policy.rules import RULES_KIND, RuleSetEvaluation, parse_rule_set
from pairly_core.policy.service import (
    RuleSetNotFoundError,
    dry_run,
    load_rule_set,
    publish_rule_set,
    results_for_evaluation,
)
from pairly_core.policy_versions import (
    PolicyError,
    latest_policies,
    latest_policy,
    list_policy_versions,
)
from pairly_core.rbac import Actor, ActorKind, PermissionDeniedError, require
from pairly_core.retrieval.embeddings import Embedder, EmbeddingUnavailable
from pairly_core.retrieval.metered import build_metered_embedder
from pairly_core.retrieval.search import hybrid_search

router = APIRouter(prefix="/policies", tags=["policies"])

ReadActor = Annotated[Actor, Depends(require(POLICY_READ))]
ManageActor = Annotated[Actor, Depends(require(POLICY_MANAGE))]
NAME_PATTERN = r"^[a-z][a-z0-9_.]{0,127}$"
PolicyName = Annotated[str, Field(pattern=NAME_PATTERN)]
NameParam = Annotated[str, PathParam(pattern=NAME_PATTERN)]
MAX_FACTS_BYTES = 64 * 1024


def get_embedder(request: Request) -> Embedder:
    """The app's embedder (``app.state.policy_embedder``), built once from settings.

    Metered: every embedding call is reserved against the org's model budget and
    recorded in ``model_calls`` (task ``core_embed``) through the model router.
    """
    embedder = getattr(request.app.state, "policy_embedder", None)
    if embedder is None:
        embedder = build_metered_embedder(
            load_llm_settings(), request.app.state.pairly.sessionmaker
        )
        request.app.state.policy_embedder = embedder
    if not isinstance(embedder, Embedder):  # pragma: no cover - configuration error
        raise TypeError("app.state.policy_embedder is not an Embedder")
    return embedder


EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


# ------------------------------------------------------------------ schemas


class PolicyVersionOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    content_hash: str
    created_at: datetime
    content: str | None = None


class PublishIn(BaseModel):
    content: Annotated[str, Field(min_length=1, max_length=64 * 1024)]


class PublishOut(BaseModel):
    created: bool
    version: PolicyVersionOut


class DryRunIn(BaseModel):
    name: PolicyName | None = None
    version: Annotated[int, Field(ge=1)] | None = None
    content: Annotated[str, Field(max_length=64 * 1024)] | None = None
    facts: dict[str, Any]
    as_of: date | None = None


class RuleResultOut(BaseModel):
    rule_id: str
    outcome: str
    severity: str | None
    reason: str
    reason_ar: str | None
    error_code: str | None
    facts: dict[str, Any]


class DryRunOut(BaseModel):
    name: str
    version: int | None
    as_of: date
    overall: str
    requires_human: bool
    results: list[RuleResultOut]


class StoredResultOut(RuleResultOut):
    id: uuid.UUID
    evaluation_id: uuid.UUID
    source: str
    policy_name: str
    policy_version: int | None
    policy_version_id: uuid.UUID | None
    evidence: list[Any]
    model_call_id: uuid.UUID | None
    subject_type: str | None
    subject_id: str | None
    created_at: datetime


class PolicyWarningOut(BaseModel):
    """One guardrail finding on a policy document, for admins."""

    id: str
    type: str
    code: str
    severity: str
    location: str
    page: int | None
    evidence: str
    excluded: bool
    blocks_activation: bool
    explanation: str
    explanation_ar: str


class PolicyDocumentOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    title: str
    document_id: uuid.UUID
    status: str
    status_reason: str | None
    chunk_count: int
    embedding_model: str | None
    embedding_version: str | None
    created_at: datetime
    indexed_at: datetime | None
    archived_at: datetime | None
    warning_count: int
    # Guardrail findings; only for callers who manage policies, else empty.
    warnings: list[PolicyWarningOut]
    # Guardrail override (two-person rule for high-severity findings). The reasons are
    # only shown to callers who manage policies.
    override_requested_by: uuid.UUID | None = None
    override_requested_at: datetime | None = None
    override_request_reason: str | None = None
    overridden_by: uuid.UUID | None = None
    overridden_at: datetime | None = None
    override_reason: str | None = None

    @classmethod
    def of(cls, row: PolicyDocument, actor: Actor) -> PolicyDocumentOut:
        findings = list(row.guardrail_findings or [])
        manager = actor.has(POLICY_MANAGE)
        return cls(
            id=row.id,
            name=row.name,
            version=row.version,
            title=row.title,
            document_id=row.document_id,
            status=row.status,
            status_reason=row.status_reason,
            chunk_count=row.chunk_count,
            embedding_model=row.embedding_model,
            embedding_version=row.embedding_version,
            created_at=row.created_at,
            indexed_at=row.indexed_at,
            archived_at=row.archived_at,
            warning_count=len(findings),
            warnings=([PolicyWarningOut.model_validate(f) for f in findings] if manager else []),
            override_requested_by=row.override_requested_by,
            override_requested_at=row.override_requested_at,
            override_request_reason=row.override_request_reason if manager else None,
            overridden_by=row.overridden_by,
            overridden_at=row.overridden_at,
            override_reason=row.override_reason if manager else None,
        )


class PolicyUploadOut(BaseModel):
    policy_document: PolicyDocumentOut
    document: DocumentOut
    duplicate: bool


class IndexOut(BaseModel):
    status: str
    reason: str | None
    chunks: int
    policy_document: PolicyDocumentOut


class OverrideIn(BaseModel):
    reason: Annotated[str, Field(min_length=10, max_length=2000)]


class OverrideOut(BaseModel):
    # requested: waiting for a second admin. overridden: approved, then indexed.
    status: str
    second_admin_required: bool
    index: IndexOut | None
    policy_document: PolicyDocumentOut


class SearchIn(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    names: Annotated[list[PolicyName], Field(max_length=50)] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 8


class SearchHitOut(BaseModel):
    chunk_id: uuid.UUID
    policy_document_id: uuid.UUID
    name: str
    title: str
    version: int
    page_start: int | None
    page_end: int | None
    section: str | None
    text: str
    score: float


# ------------------------------------------------------------------ rule sets


def _version_out(policy: Any, *, content: bool = False) -> PolicyVersionOut:
    return PolicyVersionOut(
        id=policy.id,
        name=policy.name,
        version=policy.version,
        content_hash=policy.content_hash,
        created_at=policy.created_at,
        content=policy.content if content else None,
    )


@router.get("/rule-sets", response_model=list[PolicyVersionOut])
async def list_rule_sets(
    actor: ReadActor,
    session: OrgSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[PolicyVersionOut]:
    """Every rule set in the organisation: its name and latest version, by name."""
    return [_version_out(p) for p in await latest_policies(session, RULES_KIND, limit=limit)]


@router.get("/rule-sets/{name}", response_model=PolicyVersionOut)
async def get_rule_set(
    name: NameParam,
    actor: ReadActor,
    session: OrgSession,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> PolicyVersionOut:
    try:
        policy, _ = await load_rule_set(session, name, version)
    except RuleSetNotFoundError:
        raise NotFoundError("rule set not found") from None
    return _version_out(policy, content=True)


@router.get("/rule-sets/{name}/versions", response_model=list[PolicyVersionOut])
async def list_rule_set_versions(
    name: NameParam, actor: ReadActor, session: OrgSession
) -> list[PolicyVersionOut]:
    versions = await list_policy_versions(session, RULES_KIND, name)
    return [_version_out(v) for v in versions]


@router.post(
    "/rule-sets/{name}/versions",
    status_code=201,
    response_model=PublishOut,
    responses={200: {"model": PublishOut, "description": "Identical to the latest version"}},
)
async def publish_rule_set_version(
    name: NameParam, body: PublishIn, actor: ManageActor, session: OrgSession
) -> Any:
    kind, actor_id = actor_ref(actor)
    before = await latest_policy(session, RULES_KIND, name)
    try:
        policy = await publish_rule_set(
            session, name=name, content=body.content, actor_type=kind, actor_id=actor_id
        )
    except PolicyError as exc:
        raise BadRequestError(str(exc)) from None
    created = before is None or before.id != policy.id
    out = PublishOut(created=created, version=_version_out(policy, content=True))
    if created:
        return out
    return JSONResponse(out.model_dump(mode="json"), status_code=200)


def _dry_run_out(evaluation: RuleSetEvaluation, version: int | None) -> DryRunOut:
    return DryRunOut(
        name=evaluation.name,
        version=version,
        as_of=evaluation.as_of,
        overall=evaluation.overall.value,
        requires_human=evaluation.requires_human,
        results=[
            RuleResultOut(
                rule_id=r.rule_id,
                outcome=r.outcome.value,
                severity=r.severity.value,
                reason=r.reason,
                reason_ar=r.reason_ar,
                error_code=r.error_code,
                facts=r.facts,
            )
            for r in evaluation.results
        ],
    )


@router.post("/rule-sets/dry-run", response_model=DryRunOut)
async def dry_run_rule_set(body: DryRunIn, actor: ReadActor, session: OrgSession) -> DryRunOut:
    """Evaluate sample facts. Stores nothing (no results, no audit event)."""
    if len(body.model_dump_json()) > MAX_FACTS_BYTES + 64 * 1024:
        raise BadRequestError("the request is too large")
    as_of = body.as_of or datetime.now(UTC).date()
    version: int | None = None
    try:
        if body.content is not None:
            if body.name is not None or body.version is not None:
                raise BadRequestError("send either content or name, not both")
            rule_set = parse_rule_set(body.content)
        elif body.name is not None:
            policy, rule_set = await load_rule_set(session, body.name, body.version)
            version = policy.version
        else:
            raise BadRequestError("send content or name")
    except RuleSetNotFoundError:
        raise NotFoundError("rule set not found") from None
    except PolicyError as exc:
        raise BadRequestError(str(exc)) from None
    return _dry_run_out(dry_run(rule_set, body.facts, as_of=as_of), version)


@router.get("/rule-results", response_model=list[StoredResultOut])
async def get_rule_results(
    actor: ReadActor, session: OrgSession, evaluation_id: uuid.UUID
) -> list[StoredResultOut]:
    rows = await results_for_evaluation(session, evaluation_id)
    return [_stored_out(r) for r in rows]


def _stored_out(r: RuleResult) -> StoredResultOut:
    return StoredResultOut(
        id=r.id,
        evaluation_id=r.evaluation_id,
        source=r.source,
        policy_name=r.policy_name,
        policy_version=r.policy_version,
        policy_version_id=r.policy_version_id,
        rule_id=r.rule_id,
        outcome=r.outcome,
        severity=r.severity,
        reason=r.reason,
        reason_ar=r.reason_ar,
        error_code=r.error_code,
        facts=dict(r.facts or {}),
        evidence=list(r.evidence or []),
        model_call_id=r.model_call_id,
        subject_type=r.subject_type,
        subject_id=r.subject_id,
        created_at=r.created_at,
    )


# ------------------------------------------------------------------ documents


@router.post(
    "/documents",
    status_code=201,
    response_model=PolicyUploadOut,
    responses={
        200: {"model": PolicyUploadOut, "description": "Same file already registered"},
        413: {"description": "File too large"},
        415: {"description": "Not a PDF, DOCX or text file"},
    },
)
async def upload_policy_document(
    request: Request,
    actor: ManageActor,
    session: OrgSession,
    background: BackgroundTasks,
    name: Annotated[str, Query(pattern=NAME_PATTERN)],
    title: Annotated[str, Query(min_length=1, max_length=200)],
) -> Any:
    services = get_services(request)
    policy = policy_intake(services.settings)
    by = AuditBy.from_actor(actor)
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        await record_rejection(
            session,
            by=by,
            workflow=POLICY_WORKFLOW,
            source=DocumentSource.UPLOAD,
            reason=RejectReason.TOO_LARGE,
        )
        return rejected_response(request, RejectReason.TOO_LARGE, POLICY_WORKFLOW)
    await session.commit()
    with tempfile.TemporaryDirectory(prefix="pairly-policy-", dir=services.tmp_dir) as tmp:
        try:
            spooled, filename = await receive_upload(
                request, Path(tmp), policy.max_bytes, services.settings.upload_timeout_seconds
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
                workflow=POLICY_WORKFLOW,
                source=DocumentSource.UPLOAD,
                reason=RejectReason.TOO_LARGE,
            )
            return rejected_response(request, RejectReason.TOO_LARGE, POLICY_WORKFLOW)
        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=spooled,
            workflow=POLICY_WORKFLOW,
            source=DocumentSource.UPLOAD,
            filename=filename,
            policy=policy,
            by=by,
            submitted_by=actor.id,
        )
    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, POLICY_WORKFLOW)
    document: Document = outcome.document
    kind, actor_id = actor_ref(actor)
    try:
        row = await register_policy_document(
            session,
            name=name,
            title=title.strip(),
            document=document,
            actor_type=kind,
            actor_id=actor_id,
        )
    except PolicyError as exc:
        raise BadRequestError(str(exc)) from None
    if outcome.created:
        background.add_task(enqueue_scan, services.queue, actor.org_id, document.id)
    body = PolicyUploadOut(
        policy_document=PolicyDocumentOut.of(row, actor),
        document=DocumentOut.of(document),
        duplicate=not outcome.created,
    )
    if outcome.created:
        return body
    return JSONResponse(body.model_dump(mode="json"), status_code=200)


@router.get("/documents", response_model=list[PolicyDocumentOut])
async def list_documents(
    actor: ReadActor,
    session: OrgSession,
    name: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
) -> list[PolicyDocumentOut]:
    rows = await list_policy_documents(session, name=name)
    return [PolicyDocumentOut.of(r, actor) for r in rows]


async def _load_document(session: OrgSession, policy_document_id: uuid.UUID) -> PolicyDocument:
    row = await get_policy_document(session, policy_document_id)
    if row is None:
        raise NotFoundError("policy document not found")
    return row


@router.get("/documents/{policy_document_id}", response_model=PolicyDocumentOut)
async def get_document(
    policy_document_id: uuid.UUID, actor: ReadActor, session: OrgSession
) -> PolicyDocumentOut:
    return PolicyDocumentOut.of(await _load_document(session, policy_document_id), actor)


@router.post("/documents/{policy_document_id}/index", response_model=IndexOut)
async def index_document(
    request: Request,
    policy_document_id: uuid.UUID,
    actor: ManageActor,
    session: OrgSession,
    embedder: EmbedderDep,
) -> IndexOut:
    services = get_services(request)
    await _load_document(session, policy_document_id)
    kind, actor_id = actor_ref(actor)
    try:
        outcome = await index_policy_document(
            session,
            services.store,
            policy_document_id=policy_document_id,
            embedder=embedder,
            actor_type=kind,
            actor_id=actor_id,
        )
    except PolicyDocumentNotFoundError:
        raise NotFoundError("policy document not found") from None
    if outcome.status == "pending":
        raise ConflictError("the document is not parsed yet; try again later")
    row = await _load_document(session, policy_document_id)
    return IndexOut(
        status=outcome.status,
        reason=outcome.reason,
        chunks=outcome.chunks,
        policy_document=PolicyDocumentOut.of(row, actor),
    )


@router.post("/documents/{policy_document_id}/archive", response_model=PolicyDocumentOut)
async def archive_document(
    policy_document_id: uuid.UUID, actor: ManageActor, session: OrgSession
) -> PolicyDocumentOut:
    await _load_document(session, policy_document_id)
    kind, actor_id = actor_ref(actor)
    row = await archive_policy_document(
        session, policy_document_id=policy_document_id, actor_type=kind, actor_id=actor_id
    )
    return PolicyDocumentOut.of(row, actor)


@router.post(
    "/documents/{policy_document_id}/override",
    response_model=OverrideOut,
    responses={202: {"model": OverrideOut, "description": "Recorded; a second admin must approve"}},
)
async def override_document(
    request: Request,
    policy_document_id: uuid.UUID,
    body: OverrideIn,
    actor: ManageActor,
    session: OrgSession,
    embedder: EmbedderDep,
) -> Any:
    """Override the guardrails of a ``needs_review`` document (step-up, written reason)."""
    if actor.kind is not ActorKind.USER:
        raise PermissionDeniedError("only a person can override the guardrails")
    await _load_document(session, policy_document_id)
    outcome = await request_override(
        session, policy_document_id=policy_document_id, user_id=actor.id, reason=body.reason
    )
    second = override_needs_two_admins(outcome.row)
    if outcome.status == "requested":
        out = OverrideOut(
            status="requested",
            second_admin_required=second,
            index=None,
            policy_document=PolicyDocumentOut.of(outcome.row, actor),
        )
        return JSONResponse(out.model_dump(mode="json"), status_code=202)
    kind, actor_id = actor_ref(actor)
    index: IndexOut | None = None
    services = getattr(request.app.state, "documents", None)
    if services is not None:
        result = await index_policy_document(
            session,
            services.store,
            policy_document_id=policy_document_id,
            embedder=embedder,
            actor_type=kind,
            actor_id=actor_id,
        )
        row = await _load_document(session, policy_document_id)
        index = IndexOut(
            status=result.status,
            reason=result.reason,
            chunks=result.chunks,
            policy_document=PolicyDocumentOut.of(row, actor),
        )
    row = await _load_document(session, policy_document_id)
    return OverrideOut(
        status="overridden",
        second_admin_required=second,
        index=index,
        policy_document=PolicyDocumentOut.of(row, actor),
    )


@router.post("/search", response_model=list[SearchHitOut])
async def search_policies(
    body: SearchIn, actor: ReadActor, session: OrgSession, embedder: EmbedderDep
) -> list[SearchHitOut]:
    docs = {d.id: d for d in await active_policy_documents(session, names=body.names)}
    try:
        hits = await hybrid_search(
            session,
            kind=POLICY_KIND,
            query=body.query,
            source_ids=list(docs),
            embedder=embedder,
            limit=body.limit,
        )
    except EmbeddingUnavailable:
        raise ServiceUnavailableError("policy search is unavailable") from None
    return [
        SearchHitOut(
            chunk_id=h.chunk_id,
            policy_document_id=h.source_id,
            name=docs[h.source_id].name,
            title=docs[h.source_id].title,
            version=docs[h.source_id].version,
            page_start=h.page_start,
            page_end=h.page_end,
            section=h.section,
            text=h.text,
            score=h.score,
        )
        for h in hits
    ]
