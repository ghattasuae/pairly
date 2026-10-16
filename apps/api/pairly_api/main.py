# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""FastAPI application factory.

Run with: ``uvicorn pairly_api.main:create_app --factory``.

Startup fails closed: missing settings or encryption keys (``PAIRLY_KEK_*``,
``PAIRLY_BLIND_INDEX_KEY``) or the API key pepper (``PAIRLY_API_KEY_PEPPER``) raise
``ConfigError``, an invalid workflow plugin
raises ``PluginError``, and connecting to Postgres as a role that bypasses RLS raises
``UnsafeDatabaseRoleError``. Document storage settings (``PAIRLY_S3_*``) are required
too, except in the test environment, where the document routes answer 503 without them.
Notification settings (``pairly_core.notifications.settings``) are read at startup as
well; outside the test environment the approval hooks send approval emails, Slack
messages and in-app items (``NotificationApprovalHook``).

Workflow plugins plug in without any change here: their permissions, graphs, events,
flags, search sources and retention rules are read from the discovered plugin objects,
and a plugin's ``api_router`` is mounted under ``/v1/<plugin id>``. Graph nodes get the
engine services of this process (``GraphServices``: document store, model router, and
the scanner and parse settings when ingestion is configured).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_api import __version__
from pairly_api.decision_services import DecisionServices, build_decision_services
from pairly_api.idempotency import IdempotencyMiddleware, IdempotentReplay, replay_handler
from pairly_api.logs import configure_logging
from pairly_api.middleware import EdgeMiddleware
from pairly_api.problems import install_error_handlers
from pairly_api.routes import (
    api_keys,
    approval_links,
    approvals,
    audit,
    batches,
    compliance,
    decisions,
    dev_objects,
    flags,
    health,
    invitations,
    me,
    members,
    notification_templates,
    notifications,
    policies,
    proposals,
    retention,
    search,
    slack,
    webhooks,
)
from pairly_api.routes import documents as document_routes
from pairly_api.routes import extractions as extraction_routes
from pairly_api.telemetry import configure_tracing, instrument_app
from pairly_api.uploads import DocumentServices
from pairly_core.api_keys.ratelimit import RateLimiter
from pairly_core.api_keys.tokens import init_api_key_pepper
from pairly_core.approvals.hooks import NO_HOOKS, ApprovalHooks
from pairly_core.auth import TokenVerifier
from pairly_core.batches.intake import build_intake_registry
from pairly_core.config import ConfigError, Environment, Settings, load_settings
from pairly_core.crypto.keys import init_crypto
from pairly_core.db import check_app_role, create_engine, create_sessionmaker
from pairly_core.decisions.hooks import DecisionRecordHook
from pairly_core.flags.registry import build_flag_registry, install_flag_registry
from pairly_core.identity import ORG_HEADER, AppContext
from pairly_core.integrations.slack.transport import HttpSlackTransport, SlackTransport
from pairly_core.notifications.approval_hook import NotificationApprovalHook
from pairly_core.notifications.settings import NotificationSettings, load_notification_settings
from pairly_core.plugins import WorkflowPlugin, discover_plugins, plugin_routers
from pairly_core.rbac import RoleRegistry, build_registry
from pairly_core.retention.rules import build_retention_registry
from pairly_core.search.sources import build_search_registry
from pairly_core.webhooks.events import build_event_registry, install_event_registry
from pairly_core.webhooks.settings import load_webhook_settings
from pairly_core.workflow.checkpoint import TenantCheckpointer
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_core.workflow.services import GraphServices

log = structlog.get_logger(__name__)


async def _document_services(settings: Settings) -> DocumentServices | None:
    """Storage and queue for the document routes, from ``PAIRLY_S3_*`` and friends."""
    from pairly_core.ingestion.queue import ArqJobQueue, JobQueue, NullJobQueue
    from pairly_core.ingestion.settings import load_ingestion_settings
    from pairly_core.ingestion.storage import object_store_from_settings

    try:
        ingestion = load_ingestion_settings(env=settings.env)
    except ConfigError:
        if settings.env is Environment.TEST:
            log.warning("documents_disabled", reason="ingestion settings missing")
            return None
        raise
    queue: JobQueue = NullJobQueue()
    if ingestion.redis_url is not None:
        try:
            queue = await ArqJobQueue.connect(ingestion.redis_url.get_secret_value())
        except Exception as exc:
            # Uploads still work; the worker's sweeper queues their scans.
            log.warning("job_queue_unavailable", error=type(exc).__name__)
    return DocumentServices(
        settings=ingestion, store=object_store_from_settings(ingestion), queue=queue
    )


def _graph_services(
    sessionmaker: async_sessionmaker[AsyncSession], documents: DocumentServices | None
) -> GraphServices:
    """What graph nodes may use in this process: the model router (recorded mode unless
    configured otherwise), the metered embedder (policy search) and, when document
    storage is configured, the store, the
    scanner (only when ClamAV or the development fake is configured) and the sandboxed
    parse settings."""
    from pairly_core.documents.pipeline import ParseConfig
    from pairly_core.ingestion.scan import scanner_from_settings
    from pairly_core.llm.factory import build_router
    from pairly_core.llm.settings import load_llm_settings
    from pairly_core.retrieval.metered import build_metered_embedder

    llm = load_llm_settings()
    router = build_router(llm, sessionmaker)
    embedder = build_metered_embedder(llm, sessionmaker)
    if documents is None:
        return GraphServices(router=router, embedder=embedder)
    ingestion = documents.settings
    return GraphServices(
        store=documents.store,
        router=router,
        scanner=scanner_from_settings(ingestion),
        parse_config=ParseConfig.from_settings(ingestion),
        embedder=embedder,
    )


def _lifespan(
    settings: Settings,
    registry: RoleRegistry,
    plugins: list[WorkflowPlugin],
    hooks: ApprovalHooks,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        verifier = TokenVerifier.from_settings(settings)
        owned_services = False
        owned_decisions = False
        try:
            if getattr(app.state, "documents", None) is None:
                app.state.documents = await _document_services(settings)
                owned_services = app.state.documents is not None
            await check_app_role(engine)
            sessionmaker = create_sessionmaker(engine)
            if getattr(app.state, "decision_services", None) is None:
                app.state.decision_services = await build_decision_services(settings, sessionmaker)
                owned_decisions = True
            app.state.pairly = AppContext(
                settings=settings,
                engine=engine,
                sessionmaker=sessionmaker,
                verifier=verifier,
                registry=registry,
            )
            services = getattr(app.state, "graph_services", None)
            if not isinstance(services, GraphServices):
                services = _graph_services(sessionmaker, app.state.documents)
            runtime = WorkflowRuntime.from_plugins(
                plugins,
                sessionmaker=sessionmaker,
                checkpointer=TenantCheckpointer(settings.database_url.get_secret_value()),
                registry=registry,
                # Decision records first: a hook that raises aborts the change.
                hooks=ApprovalHooks([DecisionRecordHook(), hooks]),
                services=services,
            )
            await runtime.check()
            app.state.workflow_runtime = runtime
            log.info("startup", env=settings.env.value, permissions=len(registry.permissions))
            yield
        finally:
            services = getattr(app.state, "documents", None)
            close = getattr(getattr(services, "queue", None), "aclose", None)
            if owned_services and close is not None:
                await close()
            close_slack = getattr(getattr(app.state, "slack_transport", None), "aclose", None)
            if close_slack is not None:
                await close_slack()
            decision_queue = getattr(getattr(app.state, "decision_services", None), "queue", None)
            close_queue = getattr(decision_queue, "aclose", None)
            if owned_decisions and close_queue is not None:
                await close_queue()
            await verifier.aclose()
            await engine.dispose()

    return lifespan


def _serves_dev_objects(settings: Settings, documents: DocumentServices | None) -> bool:
    """Mount ``/v1/_dev/objects`` only in development and test, with the filesystem store."""
    if settings.env not in {Environment.DEVELOPMENT, Environment.TEST}:
        return False
    if documents is not None:
        from pairly_core.ingestion.fs_storage import FilesystemObjectStore

        return isinstance(documents.store, FilesystemObjectStore)
    return os.environ.get("PAIRLY_OBJECT_STORE", "").strip().lower() == "filesystem"


def default_approval_hooks(settings: Settings, notify: NotificationSettings) -> ApprovalHooks:
    """Approval notifications everywhere but in tests, which pass their own hooks."""
    if settings.env is Environment.TEST:
        return NO_HOOKS
    return ApprovalHooks(
        [NotificationApprovalHook(notify, link_ttl_seconds=settings.approval_link_ttl_seconds)]
    )


def create_app(
    settings: Settings | None = None,
    *,
    plugins: Iterable[WorkflowPlugin] | None = None,
    documents: DocumentServices | None = None,
    approval_hooks: ApprovalHooks | None = None,
    notification_settings: NotificationSettings | None = None,
    slack_transport: SlackTransport | None = None,
    decision_services: DecisionServices | None = None,
    graph_services: GraphServices | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    notify = notification_settings or load_notification_settings(env=settings.env)
    if approval_hooks is None:
        approval_hooks = default_approval_hooks(settings, notify)
    init_crypto()
    init_api_key_pepper()
    webhook_settings = load_webhook_settings()
    configure_logging(settings.log_level)
    configure_tracing()
    workflow_plugins = list(discover_plugins().values()) if plugins is None else list(plugins)
    registry = build_registry(workflow_plugins)
    install_event_registry(build_event_registry(workflow_plugins))
    install_flag_registry(build_flag_registry(workflow_plugins))

    show_docs = not settings.is_production
    app = FastAPI(
        title="Pairly API",
        version=__version__,
        lifespan=_lifespan(settings, registry, workflow_plugins, approval_hooks),
        openapi_url="/v1/openapi.json",
        docs_url="/docs" if show_docs else None,
        redoc_url=None,
    )
    install_error_handlers(app)
    app.add_exception_handler(IdempotentReplay, replay_handler)
    app.state.documents = documents
    app.state.notifications = notify
    app.state.slack_transport = slack_transport or HttpSlackTransport(
        base_url=notify.slack_api_base_url
    )
    app.state.decision_services = decision_services
    app.state.graph_services = graph_services
    app.state.webhook_settings = webhook_settings
    app.state.search_registry = build_search_registry(workflow_plugins)
    app.state.retention_registry = build_retention_registry(workflow_plugins)
    app.state.intake_registry = build_intake_registry(workflow_plugins)
    app.state.batch_limiter = RateLimiter()

    v1 = APIRouter(prefix="/v1")
    v1.include_router(me.router)
    v1.include_router(audit.router)
    v1.include_router(invitations.router)
    v1.include_router(members.router)
    # Before the documents router: /documents/batch is not a document id.
    v1.include_router(batches.router)
    v1.include_router(document_routes.router)
    v1.include_router(extraction_routes.router)
    v1.include_router(approvals.router)
    v1.include_router(approval_links.router)
    v1.include_router(policies.router)
    v1.include_router(notifications.router)
    v1.include_router(notification_templates.router)
    v1.include_router(slack.router)
    v1.include_router(decisions.router)
    v1.include_router(compliance.router)
    v1.include_router(api_keys.router)
    v1.include_router(webhooks.router)
    v1.include_router(flags.router)
    v1.include_router(search.router)
    v1.include_router(retention.router)
    v1.include_router(proposals.router)
    if _serves_dev_objects(settings, documents):
        # Signed URLs of the filesystem object store (native development, no MinIO).
        v1.include_router(dev_objects.router)
    # Workflow plugin routes, each under /v1/<plugin id> (pairly_core.plugins).
    for plugin_id, plugin_router in plugin_routers(workflow_plugins):
        v1.include_router(plugin_router, prefix=f"/{plugin_id}")
    app.include_router(health.router)
    app.include_router(v1)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                ORG_HEADER,
                "X-Request-ID",
                "Idempotency-Key",
            ],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )
    instrument_app(app)
    # Inside the edge layer: hashes Idempotency-Key requests and keeps their responses.
    app.add_middleware(IdempotencyMiddleware)
    # Added last, so it is the outermost layer and wraps every response.
    app.add_middleware(EdgeMiddleware, hsts=settings.is_production)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazy ``pairly_api.main:app`` for ``uvicorn pairly_api.main:app``.

    Built on first access, so importing this module never reads settings.
    """
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
