"""The manifest: how a client finds a running bridge.

It carries the bearer token and OpenCode's password, so it is written 0600
before any content exists and removed when the supervisor stops cleanly.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_serializer

from tandem.supervisor.state import write_private


class Manifest(BaseModel):
    version: Literal[1] = 1
    host: str
    port: int
    token: SecretStr
    task_dir: str
    state_dir: str
    pid: int = Field(default_factory=os.getpid)
    opencode_port: int | None = None
    opencode_version: str | None = None
    # Lets a human attach OpenCode's own TUI or web UI to the live session.
    opencode_password: SecretStr | None = None
    started_at: float = Field(default_factory=time.time)

    @field_serializer("token", "opencode_password", when_used="json")
    def _reveal_secret(self, secret: SecretStr | None) -> str | None:
        # The manifest file is the one place these are meant to be written.
        return secret.get_secret_value() if secret is not None else None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def dashboard_url(self) -> str:
        # The token rides in the fragment, which a browser never sends anywhere.
        return f"{self.url}/ui#token={self.token.get_secret_value()}"

    @property
    def opencode_url(self) -> str | None:
        return f"http://127.0.0.1:{self.opencode_port}" if self.opencode_port else None

    @property
    def alive(self) -> bool:
        """Whether the process that wrote this manifest still exists."""
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def write(self, path: Path) -> None:
        write_private(path, self.model_dump_json(indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_bytes())

    @staticmethod
    def remove(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
