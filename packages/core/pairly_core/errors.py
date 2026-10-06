# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Error types that map onto the API problem+json error model (RFC 9457).

Core code raises these; the API turns them into ``application/problem+json`` responses.
``detail`` is shown to clients, so it must never contain secrets, tokens or personal data.
"""

from __future__ import annotations

from typing import ClassVar


class PairlyError(Exception):
    """Base error with an HTTP status and a stable machine-readable code."""

    status: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    title: ClassVar[str] = "Internal error"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title


class BadRequestError(PairlyError):
    status = 400
    code = "bad_request"
    title = "Bad request"


class UnauthenticatedError(PairlyError):
    status = 401
    code = "unauthenticated"
    title = "Authentication required"


class ForbiddenError(PairlyError):
    status = 403
    code = "forbidden"
    title = "Forbidden"


class NotFoundError(PairlyError):
    status = 404
    code = "not_found"
    title = "Not found"


class ServiceUnavailableError(PairlyError):
    status = 503
    code = "service_unavailable"
    title = "Service unavailable"


class ConflictError(PairlyError):
    status = 409
    code = "conflict"
    title = "Conflict"
