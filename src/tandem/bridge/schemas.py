"""Request and response bodies for the bridge.

Validation lives here, in types, rather than in hand-written checks inside each
handler. An id that could carry a path separator into an OpenCode URL is
rejected by its pattern before any handler runs.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from tandem.opencode.models import FileDiff, Message, PermissionReply
from tandem.supervisor.results import ImplementerResult
from tandem.supervisor.state import DirectiveAction, SpecStatus
from tandem.supervisor.supervisor import StatusReport, WaitReason


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")]
"""A Spec or directive id: readable, bounded, and safe in a file name."""

Text = Annotated[str, StringConstraints(max_length=60_000), AfterValidator(_not_blank)]

# OpenCode request ids are a type prefix and an opaque token. Pinning that shape
# keeps a caller-supplied id from reaching OpenCode as a path fragment.
PermissionId = Annotated[str, StringConstraints(pattern=r"^per[A-Za-z0-9_]+$")]
QuestionId = Annotated[str, StringConstraints(pattern=r"^que[A-Za-z0-9_]+$")]


class SpecIn(BaseModel):
    spec_id: Identifier
    text: Text


class PromptIn(BaseModel):
    text: Text


class DirectiveIn(BaseModel):
    spec_id: Identifier
    directive_id: Identifier
    action: DirectiveAction
    text: Text


class RetractIn(BaseModel):
    spec_id: Identifier
    directive_id: Identifier


class PermissionIn(BaseModel):
    id: PermissionId
    reply: PermissionReply
    message: Annotated[str, StringConstraints(max_length=2_000)] | None = None


class AnswerIn(BaseModel):
    """Selected labels per question, or ``reject: true`` to decline the question."""

    id: QuestionId
    answers: list[list[str]] | None = None
    reject: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if self.reject == bool(self.answers):
            raise ValueError("send either a non-empty 'answers' or 'reject': true")
        return self


class Ok(BaseModel):
    ok: bool = True
    detail: str = ""


class SpecOut(BaseModel):
    spec_id: str
    session_id: str
    status: SpecStatus
    result_path: str


class DirectiveOut(BaseModel):
    spec_id: str
    directive_id: str
    action: DirectiveAction
    retracted: bool


class WaitOut(BaseModel):
    reason: WaitReason
    detail: str
    status: StatusReport


class ResultOut(BaseModel):
    path: str | None
    result: ImplementerResult | None


class MessageOut(BaseModel):
    """A message flattened for reading on a small screen."""

    role: str
    text: str
    tools: list[str] = Field(default_factory=list[str])

    @classmethod
    def of(cls, message: Message) -> MessageOut:
        tools = [part.tool for part in message.parts if part.type == "tool" and part.tool]
        return cls(role=message.info.role, text=message.text(), tools=tools)


class MessagesOut(BaseModel):
    session_id: str
    messages: list[MessageOut]


class DiffOut(BaseModel):
    files: list[FileDiff]
