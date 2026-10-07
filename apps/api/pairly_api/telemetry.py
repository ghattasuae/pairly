# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""OpenTelemetry wiring. Spans are exported only when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

from __future__ import annotations

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

_configured = False


def configure_tracing(service_name: str = "pairly-api") -> bool:
    """Install an exporting tracer provider once per process. Returns True if exporting."""
    global _configured
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    if _configured:
        return True
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})
    provider = TracerProvider(resource=resource)
    # Exporter reads endpoint, headers and timeouts from the standard OTEL_* variables.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    return True


def instrument_app(app: FastAPI) -> None:
    """Instrument requests. Without an exporting provider the spans are no-ops."""
    # approval-links paths carry a secret token, so they are never traced.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,v1/approval-links")
