# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""FastAPI application factory.

Run with: ``uvicorn pairly_api.main:create_app --factory``.

Startup fails closed: missing settings raise ``ConfigError``, an invalid workflow plugin
raises ``PluginError``, and connecting to Postgres as a role that bypasses RLS raises
``UnsafeDatabaseRoleError``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pairly_api import __version__
from pairly_api.logs import configure_logging
from pairly_api.middleware import EdgeMiddleware
from pairly_api.problems import install_error_handlers
from pairly_api.routes import audit, health, invitations, me, members
from pairly_api.telemetry import configure_tracing, instrument_app
from pairly_core.auth import TokenVerifier
from pairly_core.config import Settings, load_settings
from pairly_core.db import check_app_role, create_engine, create_sessionmaker
from pairly_core.identity import ORG_HEADER, AppContext
from pairly_core.plugins import WorkflowPlugin, discover_plugins
from pairly_core.rbac import RoleRegistry, build_registry

log = structlog.get_logger(__name__)


def _lifespan(
    settings: Settings, registry: RoleRegistry
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        verifier = TokenVerifier.from_settings(settings)
        try:
            await check_app_role(engine)
            app.state.pairly = AppContext(
                settings=settings,
                engine=engine,
                sessionmaker=create_sessionmaker(engine),
                verifier=verifier,
                registry=registry,
            )
            log.info("startup", env=settings.env.value, permissions=len(registry.permissions))
            yield
        finally:
            await verifier.aclose()
            await engine.dispose()

    return lifespan


def create_app(
    settings: Settings | None = None,
    *,
    plugins: Iterable[WorkflowPlugin] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    configure_tracing()
    workflow_plugins = list(discover_plugins().values()) if plugins is None else list(plugins)
    registry = build_registry(workflow_plugins)

    show_docs = not settings.is_production
    app = FastAPI(
        title="Pairly API",
        version=__version__,
        lifespan=_lifespan(settings, registry),
        openapi_url="/v1/openapi.json",
        docs_url="/docs" if show_docs else None,
        redoc_url=None,
    )
    install_error_handlers(app)

    v1 = APIRouter(prefix="/v1")
    v1.include_router(me.router)
    v1.include_router(audit.router)
    v1.include_router(invitations.router)
    v1.include_router(members.router)
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
