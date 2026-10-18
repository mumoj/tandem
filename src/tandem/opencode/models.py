"""The slice of OpenCode's server API this package reads.

Shapes come from the OpenAPI document ``opencode serve`` publishes at ``/doc``
(checked against 1.19.3). Every model allows extra fields: OpenCode adds
properties between minor versions, and a new field should never be what stops
a supervisor mid-task. Only the fields something here acts on are declared.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Health(_Model):
    healthy: bool
    version: str


class CacheUsage(_Model):
    read: float = 0
    write: float = 0


class TokenUsage(_Model):
    input: float = 0
    output: float = 0
    reasoning: float = 0
    cache: CacheUsage = Field(default_factory=CacheUsage)


class Session(_Model):
    id: str
    title: str = ""
    cost: float = 0
    tokens: TokenUsage | None = None


class SessionStatus(_Model):
    """``idle``, ``busy`` or ``retry``. Kept open in case a later version adds one."""

    type: str
    attempt: int | None = None
    message: str | None = None


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionRule(_Model):
    """One entry in a session's permission ruleset.

    OpenCode evaluates the rules in order and the **last** match wins, so a broad
    rule has to come before the narrow one that overrides it. Verified against
    1.19.3: ``[ask hello.txt, allow *]`` lets the edit through unasked.
    """

    permission: str
    pattern: str
    action: PermissionAction


PermissionReply = Literal["once", "always", "reject"]


class PermissionRequest(_Model):
    id: str
    session_id: str = Field(alias="sessionID")
    permission: str
    patterns: list[str] = Field(default_factory=list[str])
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])


class QuestionOption(_Model):
    label: str
    description: str = ""


class QuestionInfo(_Model):
    question: str
    header: str = ""
    options: list[QuestionOption] = Field(default_factory=list[QuestionOption])
    multiple: bool = False


class QuestionRequest(_Model):
    id: str
    session_id: str = Field(alias="sessionID")
    questions: list[QuestionInfo] = Field(default_factory=list[QuestionInfo])


class FileDiff(_Model):
    file: str | None = None
    additions: float = 0
    deletions: float = 0
    status: str | None = None


class MessageInfo(_Model):
    id: str
    role: str
    cost: float | None = None
    tokens: TokenUsage | None = None
    model_id: str | None = Field(default=None, alias="modelID")
    provider_id: str | None = Field(default=None, alias="providerID")


class Part(_Model):
    type: str
    text: str | None = None
    tool: str | None = None


class Message(_Model):
    info: MessageInfo
    parts: list[Part] = Field(default_factory=list[Part])

    def text(self) -> str:
        """The message's visible text, joined across its text parts."""
        return "\n".join(part.text for part in self.parts if part.type == "text" and part.text)


class ModelRef(_Model):
    """``provider/model`` split the way the prompt endpoint wants it."""

    provider_id: str = Field(alias="providerID")
    model_id: str = Field(alias="modelID")

    @classmethod
    def parse(cls, value: str) -> Self:
        provider, _, model = value.partition("/")
        if not provider or not model:
            raise ValueError(f"expected provider/model, got {value!r}")
        return cls(providerID=provider, modelID=model)
