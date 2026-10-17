# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Structured JSON logging (structlog), shared by our code and the standard library.

Every event goes through ``redact_log_event`` (PII redaction) before it is rendered.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace

from pairly_core.guardrails.pii import redact_log_event


def _add_trace_ids(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def configure_logging(level: str = "INFO") -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_ids,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            # Last before rendering, so messages, fields and tracebacks are all covered.
            redact_log_event,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    # We write our own access line (without query strings, which can hold personal data).
    logging.getLogger("uvicorn.access").disabled = True
