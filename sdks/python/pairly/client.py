# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``Pairly`` (sync) and ``AsyncPairly`` (async): the same surface, one implementation.

Each resource method builds a ``Call`` (``pairly._http``); the sync client runs it with
``httpx.Client`` and the async one with ``httpx.AsyncClient``, both with the same retry
policy. Anything the helpers do not cover is one ``request`` away, and cursor-paged
lists are one ``paginate`` away.

    from pairly import Pairly

    pairly = Pairly(api_key="pk_test_...")
    job = pairly.documents.submit("invoice.pdf", workflow="pay")
    for approval in pairly.approvals.list_mine():
        print(approval["title"], approval["sla_state"])

People decide in Pairly: an API key can read, propose and upload, never approve.
"""

from __future__ import annotations

import asyncio
import builtins
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Final, Literal, Self, cast

import httpx

from pairly._http import (
    Call,
    ClientConfig,
    RetryPolicy,
    decode,
    make_config,
    retry_after_seconds,
    should_retry,
)
from pairly.errors import PairlyConnectionError, PairlyError
from pairly.types import (
    Approval,
    ApprovalPage,
    Batch,
    Proposal,
    ProposalPage,
    Submission,
)

type FileInput = str | os.PathLike[str] | bytes | IO[bytes]
ApprovalStatus = Literal["pending", "approved", "rejected", "changes_requested", "cancelled"]
DEFAULT_TIMEOUT: Final = 60.0
DECISION_ID_RE: Final = re.compile(r"^dec_[0-9A-HJKMNP-TV-Z]{26}$")


class BatchTimeoutError(PairlyError):
    """``wait_for_batch`` gave up before the batch completed."""


def _segment(value: str | uuid.UUID, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ValueError(f"{what} must be an id (a UUID)") from None


def _decision(value: str) -> str:
    if not DECISION_ID_RE.fullmatch(value):
        raise ValueError("decision_id must look like dec_ followed by 26 characters")
    return value


def _file_part(file: FileInput, filename: str | None) -> tuple[str, Any, str]:
    if isinstance(file, (str, os.PathLike)):
        path = Path(file)
        return (filename or path.name, path.read_bytes(), "application/octet-stream")
    if isinstance(file, bytes):
        return (filename or "document", file, "application/octet-stream")
    name = filename or Path(str(getattr(file, "name", "document"))).name
    return (name, file.read(), "application/octet-stream")


# ------------------------------------------------------------------ calls (shared)


def _batch_call(
    workflow: str,
    files: Sequence[tuple[FileInput, str | None]],
    urls: Sequence[str],
    metadata: Mapping[str, Any] | None,
    idempotency_key: str | None,
) -> Call:
    if not files and not urls:
        raise ValueError("give at least one file or URL")
    import json

    path = "/v1/documents/batch"
    params = {"workflow": workflow}
    if not files:
        body: dict[str, Any] = {"urls": list(urls)}
        if metadata is not None:
            body["metadata"] = dict(metadata)
        return Call("POST", path, params=params, json=body, idempotency_key=idempotency_key)
    data: dict[str, str] = {}
    if urls:
        data["urls"] = json.dumps(list(urls))
    if metadata is not None:
        data["metadata"] = json.dumps(dict(metadata))
    parts = [("file", _file_part(f, n)) for f, n in files]
    return Call(
        "POST", path, params=params, files=parts, data=data or None, idempotency_key=idempotency_key
    )


def _approvals_call(
    workflow: str | None,
    status: ApprovalStatus | None,
    assigned_to_me: bool,
    limit: int,
    cursor: str | None,
) -> Call:
    return Call(
        "GET",
        "/v1/approvals",
        params={
            "workflow": workflow,
            "status": status,
            "assigned_to_me": assigned_to_me or None,
            "limit": limit,
            "cursor": cursor,
        },
    )


def _proposal_call(
    entity_type: str,
    entity_id: str,
    body: str,
    action: str | None,
    workflow: str | None,
    idempotency_key: str | None,
) -> Call:
    payload: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "kind": "action" if action else "note",
        "body": body,
    }
    if action:
        payload["action"] = action
    if workflow:
        payload["workflow"] = workflow
    return Call("POST", "/v1/proposals", json=payload, idempotency_key=idempotency_key)


# ------------------------------------------------------------------ sync


class Pairly:
    """Synchronous client. Use it as a context manager, or call ``close``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        token: str | None = None,
        org_id: str | uuid.UUID | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        retry: RetryPolicy | None = None,
        allow_http: bool = False,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config: ClientConfig = make_config(
            api_key=api_key,
            token=token,
            org_id=org_id,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry=retry,
            allow_http=allow_http,
        )
        self._http = httpx.Client(
            base_url=self.config.base_url,
            headers=self.config.headers(),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        self._sleep = sleep
        self.documents = Documents(self)
        self.approvals = Approvals(self)
        self.proposals = Proposals(self)
        self.decisions = Decisions(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _run(self, call: Call) -> Any:
        kwargs = call.request_kwargs()
        policy = self.config.retry
        attempt = 0
        while True:
            try:
                response = self._http.request(call.method, call.path, **kwargs)
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > policy.max_retries:
                    raise PairlyConnectionError(f"could not reach the Pairly API: {exc}") from exc
                self._sleep(policy.delay(attempt, None))
                continue
            if should_retry(response) and attempt < policy.max_retries:
                attempt += 1
                self._sleep(policy.delay(attempt, retry_after_seconds(response)))
                continue
            return decode(response)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Any API call (``path`` starts with ``/v1/``), with the same retries."""
        return self._run(
            Call(method.upper(), path, params=params, json=json, idempotency_key=idempotency_key)
        )

    def paginate(
        self, path: str, params: Mapping[str, Any] | None = None, *, items: str = "items"
    ) -> Iterator[Any]:
        """Every item of a cursor-paged list (``next_cursor`` / ``cursor``)."""
        query = dict(params or {})
        while True:
            page = self._run(Call("GET", path, params=query))
            yield from page.get(items, [])
            cursor = page.get("next_cursor")
            if not cursor:
                return
            query["cursor"] = cursor


class Documents:
    def __init__(self, client: Pairly) -> None:
        self._client = client

    def submit(
        self,
        file: FileInput,
        *,
        workflow: str,
        filename: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Submission:
        """Submit one document to a workflow. Returns the job (a one-item batch): its id,
        the document and what the workflow made of it (Pay: the invoice). Decisions come
        later, by webhook (``approval.decided``, ``decision.recorded``) or polling."""
        batch = self.submit_batch(
            files=[(file, filename)],
            workflow=workflow,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        return Submission.of(batch)

    def submit_batch(
        self,
        files: Iterable[FileInput | tuple[FileInput, str | None]] = (),
        urls: Iterable[str] = (),
        *,
        workflow: str,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Batch:
        """Up to 50 files and URLs in one request; ``batch.completed`` fires when done."""
        pairs = [f if isinstance(f, tuple) else (f, None) for f in files]
        call = _batch_call(workflow, pairs, list(urls), metadata, idempotency_key)
        return cast(Batch, self._client._run(call))

    def get_batch(self, batch_id: str | uuid.UUID) -> Batch:
        path = f"/v1/batches/{_segment(batch_id, 'batch_id')}"
        return cast(Batch, self._client._run(Call("GET", path)))

    def wait_for_batch(
        self, batch_id: str | uuid.UUID, *, timeout: float = 120.0, interval: float = 1.0
    ) -> Batch:
        """Poll until the batch is completed (or raise ``BatchTimeoutError``)."""
        deadline = time.monotonic() + timeout
        while True:
            batch = self.get_batch(batch_id)
            if batch["status"] == "completed":
                return batch
            if time.monotonic() >= deadline:
                raise BatchTimeoutError(f"batch {batch_id} is still {batch['status']}")
            self._client._sleep(interval)

    def get(self, document_id: str | uuid.UUID) -> dict[str, Any]:
        path = f"/v1/documents/{_segment(document_id, 'document_id')}"
        return cast(dict[str, Any], self._client._run(Call("GET", path)))


class Approvals:
    def __init__(self, client: Pairly) -> None:
        self._client = client

    def list(
        self,
        *,
        workflow: str | None = None,
        status: ApprovalStatus | None = None,
        assigned_to_me: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ApprovalPage:
        call = _approvals_call(workflow, status, assigned_to_me, limit, cursor)
        return cast(ApprovalPage, self._client._run(call))

    def iter(
        self,
        *,
        workflow: str | None = None,
        status: ApprovalStatus | None = None,
        assigned_to_me: bool = False,
    ) -> Iterator[Approval]:
        params = _approvals_call(workflow, status, assigned_to_me, 200, None).params
        yield from self._client.paginate("/v1/approvals", params)

    def list_mine(
        self, *, workflow: str | None = None, status: ApprovalStatus | None = "pending"
    ) -> builtins.list[Approval]:
        """Every approval waiting for me (the person, or the API key's creator)."""
        return list(self.iter(workflow=workflow, status=status, assigned_to_me=True))

    def get(self, approval_id: str | uuid.UUID) -> dict[str, Any]:
        path = f"/v1/approvals/{_segment(approval_id, 'approval_id')}"
        return cast(dict[str, Any], self._client._run(Call("GET", path)))


class Proposals:
    def __init__(self, client: Pairly) -> None:
        self._client = client

    def create(
        self,
        entity_type: str,
        entity_id: str,
        body: str,
        *,
        action: str | None = None,
        workflow: str | None = None,
        idempotency_key: str | None = None,
    ) -> Proposal:
        """Suggest an action (or leave a note) for a person to decide. Changes nothing."""
        call = _proposal_call(entity_type, entity_id, body, action, workflow, idempotency_key)
        return cast(Proposal, self._client._run(call))

    def list(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        workflow: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ProposalPage:
        params = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "workflow": workflow,
            "limit": limit,
            "cursor": cursor,
        }
        return cast(ProposalPage, self._client._run(Call("GET", "/v1/proposals", params=params)))


class Decisions:
    def __init__(self, client: Pairly) -> None:
        self._client = client

    def receipt(self, decision_id: str) -> dict[str, Any]:
        """The receipt of one decision record (``dec_...``)."""
        path = f"/v1/decisions/{_decision(decision_id)}/receipt"
        return cast(dict[str, Any], self._client._run(Call("GET", path)))


# ------------------------------------------------------------------ async


class AsyncPairly:
    """Asynchronous client. Use it as an async context manager, or ``await aclose()``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        token: str | None = None,
        org_id: str | uuid.UUID | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        retry: RetryPolicy | None = None,
        allow_http: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.config: ClientConfig = make_config(
            api_key=api_key,
            token=token,
            org_id=org_id,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry=retry,
            allow_http=allow_http,
        )
        self._http = httpx.AsyncClient(
            base_url=self.config.base_url,
            headers=self.config.headers(),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        self._sleep = sleep
        self.documents = AsyncDocuments(self)
        self.approvals = AsyncApprovals(self)
        self.proposals = AsyncProposals(self)
        self.decisions = AsyncDecisions(self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _run(self, call: Call) -> Any:
        kwargs = call.request_kwargs()
        policy = self.config.retry
        attempt = 0
        while True:
            try:
                response = await self._http.request(call.method, call.path, **kwargs)
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > policy.max_retries:
                    raise PairlyConnectionError(f"could not reach the Pairly API: {exc}") from exc
                await self._sleep(policy.delay(attempt, None))
                continue
            if should_retry(response) and attempt < policy.max_retries:
                attempt += 1
                await self._sleep(policy.delay(attempt, retry_after_seconds(response)))
                continue
            return decode(response)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        return await self._run(
            Call(method.upper(), path, params=params, json=json, idempotency_key=idempotency_key)
        )

    async def paginate(
        self, path: str, params: Mapping[str, Any] | None = None, *, items: str = "items"
    ) -> AsyncIterator[Any]:
        query = dict(params or {})
        while True:
            page = await self._run(Call("GET", path, params=query))
            for item in page.get(items, []):
                yield item
            cursor = page.get("next_cursor")
            if not cursor:
                return
            query["cursor"] = cursor


class AsyncDocuments:
    def __init__(self, client: AsyncPairly) -> None:
        self._client = client

    async def submit(
        self,
        file: FileInput,
        *,
        workflow: str,
        filename: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Submission:
        batch = await self.submit_batch(
            files=[(file, filename)],
            workflow=workflow,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        return Submission.of(batch)

    async def submit_batch(
        self,
        files: Iterable[FileInput | tuple[FileInput, str | None]] = (),
        urls: Iterable[str] = (),
        *,
        workflow: str,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Batch:
        pairs = [f if isinstance(f, tuple) else (f, None) for f in files]
        call = _batch_call(workflow, pairs, list(urls), metadata, idempotency_key)
        return cast(Batch, await self._client._run(call))

    async def get_batch(self, batch_id: str | uuid.UUID) -> Batch:
        path = f"/v1/batches/{_segment(batch_id, 'batch_id')}"
        return cast(Batch, await self._client._run(Call("GET", path)))

    async def wait_for_batch(
        self, batch_id: str | uuid.UUID, *, timeout: float = 120.0, interval: float = 1.0
    ) -> Batch:
        deadline = time.monotonic() + timeout
        while True:
            batch = await self.get_batch(batch_id)
            if batch["status"] == "completed":
                return batch
            if time.monotonic() >= deadline:
                raise BatchTimeoutError(f"batch {batch_id} is still {batch['status']}")
            await self._client._sleep(interval)

    async def get(self, document_id: str | uuid.UUID) -> dict[str, Any]:
        path = f"/v1/documents/{_segment(document_id, 'document_id')}"
        return cast(dict[str, Any], await self._client._run(Call("GET", path)))


class AsyncApprovals:
    def __init__(self, client: AsyncPairly) -> None:
        self._client = client

    async def list(
        self,
        *,
        workflow: str | None = None,
        status: ApprovalStatus | None = None,
        assigned_to_me: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ApprovalPage:
        call = _approvals_call(workflow, status, assigned_to_me, limit, cursor)
        return cast(ApprovalPage, await self._client._run(call))

    async def iter(
        self,
        *,
        workflow: str | None = None,
        status: ApprovalStatus | None = None,
        assigned_to_me: bool = False,
    ) -> AsyncIterator[Approval]:
        params = _approvals_call(workflow, status, assigned_to_me, 200, None).params
        async for item in self._client.paginate("/v1/approvals", params):
            yield item

    async def list_mine(
        self, *, workflow: str | None = None, status: ApprovalStatus | None = "pending"
    ) -> builtins.list[Approval]:
        return [a async for a in self.iter(workflow=workflow, status=status, assigned_to_me=True)]

    async def get(self, approval_id: str | uuid.UUID) -> dict[str, Any]:
        path = f"/v1/approvals/{_segment(approval_id, 'approval_id')}"
        return cast(dict[str, Any], await self._client._run(Call("GET", path)))


class AsyncProposals:
    def __init__(self, client: AsyncPairly) -> None:
        self._client = client

    async def create(
        self,
        entity_type: str,
        entity_id: str,
        body: str,
        *,
        action: str | None = None,
        workflow: str | None = None,
        idempotency_key: str | None = None,
    ) -> Proposal:
        call = _proposal_call(entity_type, entity_id, body, action, workflow, idempotency_key)
        return cast(Proposal, await self._client._run(call))

    async def list(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        workflow: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ProposalPage:
        params = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "workflow": workflow,
            "limit": limit,
            "cursor": cursor,
        }
        return cast(
            ProposalPage, await self._client._run(Call("GET", "/v1/proposals", params=params))
        )


class AsyncDecisions:
    def __init__(self, client: AsyncPairly) -> None:
        self._client = client

    async def receipt(self, decision_id: str) -> dict[str, Any]:
        path = f"/v1/decisions/{_decision(decision_id)}/receipt"
        return cast(dict[str, Any], await self._client._run(Call("GET", path)))
