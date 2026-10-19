"""In-memory stand-ins for OpenCode and the clock.

``FakeOpenCode`` implements the ``OpenCodeAPI`` protocol, which is the whole
point of that seam: the supervisor's logic is tested without a process, a port,
or a model. The end-to-end test covers the real thing.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from tandem.opencode.client import NotFoundError, OpenCodeError
from tandem.opencode.events import Event
from tandem.opencode.models import (
    FileDiff,
    Health,
    Message,
    MessageInfo,
    ModelRef,
    Part,
    PermissionReply,
    PermissionRequest,
    PermissionRule,
    QuestionRequest,
    Session,
    SessionStatus,
    TokenUsage,
)


@dataclass
class FakeClock:
    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeSession:
    id: str
    title: str
    rules: list[PermissionRule]
    prompts: list[str] = field(default_factory=list[str])
    cost: float = 0.0
    tokens: TokenUsage = field(default_factory=TokenUsage)
    parent_id: str | None = None


class FakeOpenCode:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.aborted: list[str] = []
        self.permission_requests: list[PermissionRequest] = []
        self.question_requests: list[QuestionRequest] = []
        self.permission_replies: list[tuple[str, str, str | None]] = []
        self.question_replies: list[tuple[str, list[list[str]] | None]] = []
        self.statuses: dict[str, SessionStatus] = {}
        self.fail_prompts = False
        self.prompt_targets: list[tuple[ModelRef | None, str | None]] = []
        self.event_queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._ids = itertools.count(1)

    # -- helpers for tests --------------------------------------------------

    def add_subagent(self, parent_id: str) -> str:
        """A child session, as OpenCode's task tool creates one, without an event."""
        session_id = f"ses_{next(self._ids)}"
        self.sessions[session_id] = FakeSession(session_id, "subagent", [], parent_id=parent_id)
        return session_id

    def prompts(self, session_id: str) -> list[str]:
        return self.sessions[session_id].prompts

    def ask_permission(self, session_id: str, pattern: str = "instruction.md") -> str:
        request_id = f"per_{next(self._ids)}"
        self.permission_requests.append(
            PermissionRequest.model_validate(
                {
                    "id": request_id,
                    "sessionID": session_id,
                    "permission": "edit",
                    "patterns": [pattern],
                    "metadata": {},
                }
            )
        )
        return request_id

    # -- the protocol -------------------------------------------------------

    async def health(self) -> Health:
        return Health(healthy=True, version="1.19.3")

    async def create_session(self, title: str, permission: list[PermissionRule]) -> Session:
        session_id = f"ses_{next(self._ids)}"
        self.sessions[session_id] = FakeSession(session_id, title, permission)
        return Session(id=session_id, title=title)

    async def get_session(self, session_id: str) -> Session:
        session = self._session(session_id)
        return Session.model_validate(
            {
                "id": session.id,
                "title": session.title,
                "cost": session.cost,
                "tokens": session.tokens,
                "parentID": session.parent_id,
            }
        )

    async def session_status(self) -> dict[str, SessionStatus]:
        return dict(self.statuses)

    async def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        session = self._session(session_id)
        messages = [
            Message(
                info=MessageInfo(id=f"msg_{index}", role="user"),
                parts=[Part(type="text", text=text)],
            )
            for index, text in enumerate(session.prompts)
        ]
        return messages[-limit:] if limit else messages

    async def prompt(
        self, session_id: str, text: str, model: ModelRef | None, agent: str | None = None
    ) -> None:
        if self.fail_prompts:
            raise OpenCodeError("POST", f"/session/{session_id}/prompt_async", 500, "boom")
        self._session(session_id).prompts.append(text)
        self.prompt_targets.append((model, agent))

    async def abort(self, session_id: str) -> bool:
        self._session(session_id)
        self.aborted.append(session_id)
        return True

    async def permissions(self) -> list[PermissionRequest]:
        return list(self.permission_requests)

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None = None
    ) -> bool:
        self._pop(self.permission_requests, request_id)
        self.permission_replies.append((request_id, reply, message))
        return True

    async def questions(self) -> list[QuestionRequest]:
        return list(self.question_requests)

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        self._pop(self.question_requests, request_id)
        self.question_replies.append((request_id, answers))
        return True

    async def reject_question(self, request_id: str) -> bool:
        self._pop(self.question_requests, request_id)
        self.question_replies.append((request_id, None))
        return True

    async def diff(self, session_id: str) -> list[FileDiff]:
        self._session(session_id)
        return [FileDiff(file="hello.txt", additions=1, deletions=0, status="added")]

    async def events(self) -> AsyncIterator[Event]:
        while (event := await self.event_queue.get()) is not None:
            yield event

    # -- internals ----------------------------------------------------------

    def _session(self, session_id: str) -> FakeSession:
        if session_id not in self.sessions:
            raise NotFoundError("GET", f"/session/{session_id}", 404, "not found")
        return self.sessions[session_id]

    @staticmethod
    def _pop(items: list[Any], request_id: str) -> None:
        for index, item in enumerate(items):
            if item.id == request_id:
                del items[index]
                return
        raise NotFoundError("POST", request_id, 404, "no such request")


def event(kind: str, **properties: Any) -> Event:
    parsed = Event.model_validate({"id": "evt_1", "type": kind, "properties": properties})
    return parsed
