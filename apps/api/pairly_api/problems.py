# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""One error model for every response: RFC 9457 ``application/problem+json``.

Body: ``type``, ``title``, ``status``, ``detail``, ``code`` and ``request_id``. Details
never echo request input, tokens or stack traces.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from pairly_core.auth import TokenError
from pairly_core.errors import PairlyError, UnauthenticatedError

PROBLEM_JSON = "application/problem+json"


def problem_body(
    status: int,
    code: str,
    *,
    title: str | None = None,
    detail: str | None = None,
    request_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    phrase = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Error"
    body: dict[str, Any] = {
        "type": f"urn:pairly:error:{code}",
        "title": title or phrase,
        "status": status,
        "detail": detail or title or phrase,
        "code": code,
    }
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return body


def _request_id(request: Request) -> str | None:
    value = request.scope.get("state", {}).get("request_id")
    return value if isinstance(value, str) else None


def problem_response(
    request: Request,
    status: int,
    code: str,
    *,
    title: str | None = None,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body = problem_body(
        status, code, title=title, detail=detail, request_id=_request_id(request), **extra
    )
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


async def _pairly_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, PairlyError):
        raise exc
    headers = None
    if isinstance(exc, UnauthenticatedError):
        error = ', error="invalid_token"' if isinstance(exc, TokenError) else ""
        headers = {"WWW-Authenticate": f"Bearer{error}"}
    return problem_response(
        request, exc.status, exc.code, title=exc.title, detail=exc.detail, headers=headers
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    phrase = HTTPStatus(exc.status_code).phrase
    detail = exc.detail if isinstance(exc.detail, str) and exc.status_code < 500 else phrase
    code = phrase.lower().replace(" ", "_").replace("-", "_")
    return problem_response(
        request, exc.status_code, code, detail=detail, headers=dict(exc.headers or {})
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return problem_response(
        request,
        422,
        "validation_error",
        title="Validation error",
        detail="the request is not valid",
        errors=errors,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(PairlyError, _pairly_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
