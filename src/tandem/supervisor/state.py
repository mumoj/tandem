"""Durable supervisor state, and the lock that keeps one supervisor per task.

The state file is what makes a restart boring. A supervisor that dies and comes
back under the same spec adopts the session it recorded instead of creating a
duplicate, and a prompt it may or may not have sent is checked for before it
is sent again.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal, Self

from pydantic import BaseModel, Field


class SpecStatus(StrEnum):
    PROMPT_PENDING = "prompt_pending"
    """Session created; the spec prompt may not have reached it yet."""
    ACTIVE = "active"
    RESULT_WRITTEN = "result_written"
    PROTECTED_VIOLATION = "protected_violation"
    SUPERSEDED = "superseded"


class DirectiveAction(StrEnum):
    CORRECT = "CORRECT"
    """Apply a correction and carry on."""
    STOP_AND_WAIT = "STOP_AND_WAIT"
    """Abort the running turn, write a partial result, and wait."""


class DirectiveRecord(BaseModel):
    directive_id: str
    action: DirectiveAction
    text: str
    digest: str
    delivered: bool = False
    """False until the prompt is accepted; a restart re-checks undelivered ones."""
    retracted: bool = False
    created_at: float = Field(default_factory=time.time)


class SpecRecord(BaseModel):
    spec_id: str
    digest: str
    text: str
    session_id: str
    status: SpecStatus = SpecStatus.PROMPT_PENDING
    created_at: float = Field(default_factory=time.time)
    last_prompt_at: float = 0.0
    """Wall time of the latest prompt. Only a result written after it counts."""
    protected: dict[str, str] = Field(default_factory=dict[str, str])
    directives: dict[str, DirectiveRecord] = Field(default_factory=dict[str, DirectiveRecord])
    lost_sessions: list[str] = Field(default_factory=list[str])

    @property
    def active_directives(self) -> list[str]:
        return [d.directive_id for d in self.directives.values() if not d.retracted]


class SupervisorState(BaseModel):
    version: Literal[1] = 1
    task_dir: str
    current_spec_id: str | None = None
    specs: dict[str, SpecRecord] = Field(default_factory=dict[str, SpecRecord])
    updated_at: float = Field(default_factory=time.time)

    @property
    def current(self) -> SpecRecord | None:
        return self.specs.get(self.current_spec_id) if self.current_spec_id else None


class StateError(RuntimeError):
    """The state file is unreadable or belongs to a different task."""


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with a 0600 file holding ``text``.

    Written to a temporary sibling, fsynced, then renamed over the target, so a
    crash leaves either the old file or the new one and never half of each.
    The file is created 0600 before any content exists, because the manifest
    carries a bearer token.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


class StateStore:
    def __init__(self, path: Path, task_dir: Path) -> None:
        self.path = path
        self.task_dir = str(task_dir)

    def load(self) -> SupervisorState:
        if not self.path.exists():
            return SupervisorState(task_dir=self.task_dir)
        try:
            state = SupervisorState.model_validate_json(self.path.read_bytes())
        except (OSError, ValueError) as exc:
            raise StateError(f"cannot read supervisor state {self.path}: {exc}") from exc
        if state.task_dir != self.task_dir:
            raise StateError(f"{self.path} belongs to {state.task_dir}, not {self.task_dir}")
        return state

    def save(self, state: SupervisorState) -> None:
        state.updated_at = time.time()
        write_private(self.path, state.model_dump_json(indent=2) + "\n")


class SupervisorLock:
    """A non-blocking, per-task singleton lock held for the process lifetime.

    Two supervisors on one task would each create sessions and deliver every
    prompt twice. ``flock`` is released by the kernel if the process dies, so a
    crash never leaves a stale lock behind.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: IO[str] | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise RuntimeError(f"another supervisor already owns {self.path.parent}") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps({"pid": os.getpid(), "acquired": time.time()}) + "\n")
        stream.flush()
        self._stream = stream
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


def dump_jsonl(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), default=str) + "\n"
