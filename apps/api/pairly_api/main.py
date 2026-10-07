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
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pairly_api import __version__
from pairly_api.idempotency import IdempotencyMiddleware, IdempotentReplay, replay_handler
from pairly_api.logs import configure_logging
from pairly_api.middleware import EdgeMiddleware
from pairly_api.problems import install_error_handlers
from pairly_api.routes import (
    api_keys,
    approval_links,
    approvals,
    audit,
    health,
    invitations,
    me,
    members,
    policies,
    webhooks,
)
from pairly_api.routes import documents as document_routes
from pairly_api.routes import extractions as extraction_routes
from pairly_api.telemetry import configure_tracing, instrument_app
from pairly_api.uploads import DocumentServices
from pairly_core.api_keys.tokens import init_api_key_pepper
from pairly_core.approvals.hooks import NO_HOOKS, ApprovalHooks
from pairly_core.auth import TokenVerifier
from pairly_core.config import ConfigError, Environment, Settings, load_settings
from pairly_core.crypto.keys import init_crypto
from pairly_core.db import check_app_role, create_engine, create_sessionmaker
from pairly_core.identity import ORG_HEADER, AppContext
from pairly_core.plugins import WorkflowPlugin, discover_plugins
from pairly_core.rbac import RoleRegistry, build_registry
from pairly_core.webhooks.events import build_event_registry, install_event_registry
from pairly_core.webhooks.settings import load_webhook_settings
from pairly_core.workflow.checkpoint import TenantCheckpointer
from pairly_core.workflow.runtime import WorkflowRuntime

log = structlog.get_logger(__name__)


async def _document_services(settings: Settings) -> DocumentServices | None:
    """Storage and queue for the document routes, from ``PAIRLY_S3_*`` and friends."""
    from pairly_core.ingestion.queue import ArqJobQueue, JobQueue, NullJobQueue
    from pairly_core.ingestion.settings import load_ingestion_settings
    from pairly_core.ingestion.storage import S3ObjectStore

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
        settings=ingestion, store=S3ObjectStore.from_settings(ingestion), queue=queue
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
        try:
            if getattr(app.state, "documents", None) is None:
                app.state.documents = await _document_services(settings)
                owned_services = app.state.documents is not None
            await check_app_role(engine)
            sessionmaker = create_sessionmaker(engine)
            app.state.pairly = AppContext(
                settings=settings,
                engine=engine,
                sessionmaker=sessionmaker,
                verifier=verifier,
                registry=registry,
            )
            runtime = WorkflowRuntime.from_plugins(
                plugins,
                sessionmaker=sessionmaker,
                checkpointer=TenantCheckpointer(settings.database_url.get_secret_value()),
                registry=registry,
                hooks=hooks,
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
            await verifier.aclose()
            await engine.dispose()

    return lifespan


def create_app(
    settings: Settings | None = None,
    *,
    plugins: Iterable[WorkflowPlugin] | None = None,
    documents: DocumentServices | None = None,
    approval_hooks: ApprovalHooks = NO_HOOKS,
) -> FastAPI:
    settings = settings or load_settings()
    init_crypto()
    init_api_key_pepper()
    webhook_settings = load_webhook_settings()
    configure_logging(settings.log_level)
    configure_tracing()
    workflow_plugins = list(discover_plugins().values()) if plugins is None else list(plugins)
    registry = build_registry(workflow_plugins)
    install_event_registry(build_event_registry(workflow_plugins))

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
    app.state.webhook_settings = webhook_settings

    v1 = APIRouter(prefix="/v1")
    v1.include_router(me.router)
    v1.include_router(audit.router)
    v1.include_router(invitations.router)
    v1.include_router(members.router)
    v1.include_router(document_routes.router)
    v1.include_router(extraction_routes.router)
    v1.include_router(approvals.router)
    v1.include_router(approval_links.router)
    v1.include_router(policies.router)
    v1.include_router(api_keys.router)
    v1.include_router(webhooks.router)
    # Workflow plugin routers are mounted under /v1/<plugin id> in a later work package.
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
