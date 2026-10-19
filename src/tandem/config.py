"""Settings and the on-disk layout of one supervised task.

Every knob lives in one ``Settings`` class so the CLI, the environment and the
tests all configure the same object. Environment variables use the
``TANDEM_`` prefix: ``TANDEM_MODEL=openrouter/qwen/qwen3-coder``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_state_root() -> Path:
    """``$XDG_STATE_HOME/tandem``, the XDG home for logs and resumable state."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "tandem"


class Settings(BaseSettings):
    """Everything ``tandem serve`` needs to run one task."""

    model_config = SettingsConfigDict(env_prefix="TANDEM_", extra="ignore")

    task_dir: Path
    """The working tree OpenCode edits."""

    model: str | None = None
    """``provider/model`` for the implementer. ``None`` uses OpenCode's default."""

    protected: list[str] = Field(default_factory=list)
    """Glob patterns, relative to the task, that the implementer must not edit."""

    opencode_bin: str = "opencode"
    opencode_port: int = 0
    opencode_url: str | None = None
    """An ``opencode serve`` shared with other tasks. ``None`` starts one of our own."""
    opencode_password: SecretStr | None = None
    """The shared server's password. Comes from the environment, never argv."""
    bridge_port: int = 0
    state_root: Path = Field(default_factory=default_state_root)

    notify_cmd: str | None = None
    """Shell command run for every escalated event, with the summary in ``$TANDEM_NOTIFY``."""

    ntfy_topic: str | None = None
    """Opt-in push to ntfy. Off by default: it publishes summaries to a third party."""

    ntfy_server: str = "https://ntfy.sh"

    stall_after: float = 720.0
    """Seconds of text with no tool call before a busy turn counts as a reasoning stall."""

    quiet_after: float = 300.0
    """Seconds with neither text nor tools before a busy turn counts as hung."""

    text_recent: float = 120.0
    """How recent text must be for a turn to count as still thinking."""

    poll_interval: float = 2.0
    """Seconds between monitor ticks (result file, protected files, stall checks)."""

    max_stream_line: int = 128 * 1024
    notify_timeout: float = 30.0

    @field_validator("task_dir")
    @classmethod
    def _task_dir_exists(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"no such task directory: {resolved}")
        return resolved

    @model_validator(mode="after")
    def _shared_server_needs_its_password(self) -> Self:
        if (self.opencode_url is None) != (self.opencode_password is None):
            raise ValueError("opencode_url and opencode_password go together")
        return self

    @field_validator("model")
    @classmethod
    def _model_has_provider(cls, value: str | None) -> str | None:
        if value is not None and "/" not in value:
            raise ValueError("model must be provider/model, e.g. openrouter/qwen/qwen3-coder")
        return value


@dataclass(frozen=True, slots=True)
class TaskPaths:
    """Where one task's state, evidence and control files live.

    One directory per task keeps everything a reviewer needs to reconstruct a
    run in a single place, and lets ``rm -r`` reset a task cleanly.
    """

    root: Path

    @classmethod
    def for_task(cls, state_root: Path, task_dir: Path) -> TaskPaths:
        # The name is for humans typing ``--task`` on a phone; the hash keeps two
        # checkouts with the same folder name from sharing state.
        digest = hashlib.sha256(str(task_dir).encode()).hexdigest()[:6]
        return cls(state_root.expanduser() / f"{task_dir.name}-{digest}")

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def journal(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def events(self) -> Path:
        return self.root / "events.sse"

    @property
    def notifications(self) -> Path:
        return self.root / "notifications.log"

    @property
    def opencode_log(self) -> Path:
        return self.root / "opencode.log"

    @property
    def lock(self) -> Path:
        return self.root / "supervisor.lock"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def evidence(self) -> Path:
        return self.root / "sessions"

    def ensure(self) -> None:
        for directory in (self.root, self.results, self.evidence):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
