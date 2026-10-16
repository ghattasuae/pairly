# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Errors raised by the Pairly client.

Every API error body is RFC 9457 problem details (``type``, ``title``, ``status``,
``detail``, ``code``, ``request_id``). ``PairlyAPIError`` carries them; subclasses name the
common cases so callers can ``except pairly.RateLimitError``.
"""

from __future__ import annotations

from typing import Any


class PairlyError(Exception):
    """Base class for every error this package raises."""


class PairlyConfigError(PairlyError):
    """The client is not configured (no credential, a bad URL)."""


class PairlyConnectionError(PairlyError):
    """The API could not be reached (after the retries)."""


class PairlyAPIError(PairlyError):
    """The API answered with an error status."""

    def __init__(
        self,
        status: int,
        *,
        title: str,
        detail: str,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(f"{status} {title}: {detail}" if detail else f"{status} {title}")
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        self.body = body


class BadRequestError(PairlyAPIError):
    """400 or 422: the request is malformed or fails validation."""


class AuthenticationError(PairlyAPIError):
    """401: no valid API key or token."""


class PermissionDeniedError(PairlyAPIError):
    """403: the credential may not do this (API keys never approve or decide)."""


class NotFoundError(PairlyAPIError):
    """404: not found, or not visible to this credential."""


class ConflictError(PairlyAPIError):
    """409: a conflict, for example an Idempotency-Key reused with another body."""


class RateLimitError(PairlyAPIError):
    """429: too many requests; ``retry_after`` says when to try again."""


class ServerError(PairlyAPIError):
    """5xx: the API failed (after the retries)."""


def error_for(status: int) -> type[PairlyAPIError]:
    if status in {400, 422}:
        return BadRequestError
    if status == 401:
        return AuthenticationError
    if status == 403:
        return PermissionDeniedError
    if status == 404:
        return NotFoundError
    if status == 409:
        return ConflictError
    if status == 429:
        return RateLimitError
    if status >= 500:
        return ServerError
    return PairlyAPIError
