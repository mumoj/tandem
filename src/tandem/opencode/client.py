"""A typed async client for the OpenCode 1.x server API.

``OpenCodeAPI`` is the seam: the supervisor depends on that protocol, never on
HTTP, so tests swap in an in-memory fake and a future OpenCode 2 adapter is one
new class rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from types import TracebackType
from typing import Any, Protocol, Self

import httpx
from httpx_sse import aconnect_sse
from pydantic import TypeAdapter

from tandem.opencode.events import Event
from tandem.opencode.models import (
    FileDiff,
    Health,
    Message,
    ModelRef,
    PermissionReply,
    PermissionRequest,
    PermissionRule,
    QuestionRequest,
    Session,
    SessionStatus,
)


class OpenCodeError(RuntimeError):
    """OpenCode answered with an error, or could not be reached at all."""

    def __init__(self, method: str, path: str, status_code: int | None, detail: str = "") -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        where = f"{method} {path}"
        super().__init__(
            f"{where} -> {status_code}: {detail}" if status_code else f"{where}: {detail}"
        )


class NotFoundError(OpenCodeError):
    """The session, permission or question no longer exists."""


class OpenCodeAPI(Protocol):
    """Everything the supervisor needs from an OpenCode server."""

    async def health(self) -> Health: ...

    async def create_session(self, title: str, permission: list[PermissionRule]) -> Session: ...

    async def get_session(self, session_id: str) -> Session: ...

    async def session_status(self) -> dict[str, SessionStatus]: ...

    async def messages(self, session_id: str, limit: int | None = None) -> list[Message]: ...

    async def prompt(
        self, session_id: str, text: str, model: ModelRef | None, agent: str | None = None
    ) -> None: ...

    async def abort(self, session_id: str) -> bool: ...

    async def permissions(self) -> list[PermissionRequest]: ...

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None = None
    ) -> bool: ...

    async def questions(self) -> list[QuestionRequest]: ...

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool: ...

    async def reject_question(self, request_id: str) -> bool: ...

    async def diff(self, session_id: str) -> list[FileDiff]: ...

    def events(self) -> AsyncIterator[Event]: ...


_STATUSES = TypeAdapter(dict[str, SessionStatus])
_MESSAGES = TypeAdapter(list[Message])
_PERMISSIONS = TypeAdapter(list[PermissionRequest])
_QUESTIONS = TypeAdapter(list[QuestionRequest])
_DIFFS = TypeAdapter(list[FileDiff])
_BOOL = TypeAdapter(bool)


class OpenCodeClient:
    """HTTP access to one ``opencode serve`` process.

    Concurrency is capped by the connection pool rather than a lock. An
    exclusive lock in the first version let one slow call, a multi-megabyte
    evidence capture, stall the supervisor, the bridge and permission replies
    behind it.
    """

    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        username: str = "opencode",
        timeout: float = 60.0,
        max_connections: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=(username, password),
            timeout=httpx.Timeout(timeout, connect=5.0),
            limits=httpx.Limits(max_connections=max_connections),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise OpenCodeError(method, path, None, f"{type(exc).__name__}: {exc}") from exc
        if response.is_error:
            error = NotFoundError if response.status_code == 404 else OpenCodeError
            raise error(method, path, response.status_code, response.text[:300])
        return response

    async def health(self) -> Health:
        response = await self._request("GET", "/global/health")
        return Health.model_validate_json(response.content)

    async def create_session(self, title: str, permission: list[PermissionRule]) -> Session:
        body = {
            "title": title,
            "permission": [rule.model_dump(mode="json") for rule in permission],
        }
        response = await self._request("POST", "/session", json=body)
        return Session.model_validate_json(response.content)

    async def get_session(self, session_id: str) -> Session:
        response = await self._request("GET", f"/session/{session_id}")
        return Session.model_validate_json(response.content)

    async def session_status(self) -> dict[str, SessionStatus]:
        response = await self._request("GET", "/session/status")
        return _STATUSES.validate_json(response.content)

    async def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        params = {"limit": limit} if limit else None
        response = await self._request("GET", f"/session/{session_id}/message", params=params)
        return _MESSAGES.validate_json(response.content)

    async def prompt(
        self, session_id: str, text: str, model: ModelRef | None, agent: str | None = None
    ) -> None:
        """Queue a prompt and return at once. The turn runs on the server."""
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model is not None:
            body["model"] = model.model_dump(by_alias=True)
        if agent is not None:
            body["agent"] = agent
        await self._request("POST", f"/session/{session_id}/prompt_async", json=body)

    async def abort(self, session_id: str) -> bool:
        response = await self._request("POST", f"/session/{session_id}/abort")
        return _BOOL.validate_json(response.content)

    async def permissions(self) -> list[PermissionRequest]:
        response = await self._request("GET", "/permission")
        return _PERMISSIONS.validate_json(response.content)

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None = None
    ) -> bool:
        body: dict[str, Any] = {"reply": reply}
        if message:
            body["message"] = message
        response = await self._request("POST", f"/permission/{request_id}/reply", json=body)
        return _BOOL.validate_json(response.content)

    async def questions(self) -> list[QuestionRequest]:
        response = await self._request("GET", "/question")
        return _QUESTIONS.validate_json(response.content)

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        response = await self._request(
            "POST", f"/question/{request_id}/reply", json={"answers": answers}
        )
        return _BOOL.validate_json(response.content)

    async def reject_question(self, request_id: str) -> bool:
        response = await self._request("POST", f"/question/{request_id}/reject")
        return _BOOL.validate_json(response.content)

    async def diff(self, session_id: str) -> list[FileDiff]:
        response = await self._request("GET", f"/session/{session_id}/diff")
        return _DIFFS.validate_json(response.content)

    async def dispose(self) -> None:
        await self._request("POST", "/instance/dispose")

    async def events(self) -> AsyncIterator[Event]:
        """One connection to ``/event``. Returns when the server closes it.

        Reconnecting is the caller's decision: the supervisor needs to resync
        what it missed while disconnected, which a transparent retry here would
        hide from it.
        """
        timeout = httpx.Timeout(None, connect=5.0)
        try:
            async with aconnect_sse(self._http, "GET", "/event", timeout=timeout) as source:
                source.response.raise_for_status()
                async for message in source.aiter_sse():
                    if message.data and (event := Event.parse(message.data)) is not None:
                        yield event
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise OpenCodeError("GET", "/event", status, "event stream refused") from exc
        except httpx.HTTPError as exc:
            raise OpenCodeError("GET", "/event", None, f"{type(exc).__name__}: {exc}") from exc
